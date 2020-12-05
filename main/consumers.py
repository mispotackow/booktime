import aiohttp
import aioredis
import asyncio
import logging
import json
from django.conf import settings
from django.urls import reverse
from django.shortcuts import get_object_or_404
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.exceptions import StopConsumer
from channels.generic.http import AsyncHttpConsumer
from . import models

logger = logging.getLogger(__name__)


class ChatConsumer(AsyncJsonWebsocketConsumer):
    """
    Мы унаследовали наш потребитель от AsyncJsonWebsocketConsumer, который
    заботится о низкоуровневых аспектах WebSocket и кодировке JSON.
    Нам нужно реализовать receive_json(), connect() и disconnect(),
    чтобы этот класс работал.
    """
    EMPLOYEE = 2
    CLIENT = 1

    def get_user_type(self, user, order_id):
        """
        Метод get_user_type(), помимо проверки типа пользователя,
        сохраняет имя последнего сотрудника,
        с которым разговаривал клиент, в заказе.
        """
        order = get_object_or_404(models.Order, pk=order_id)

        if user.is_employee:
            order.last_spoken_to = user
            order.save()
            return ChatConsumer.EMPLOYEE
        elif order.user == user:
            return ChatConsumer.CLIENT
        else:
            return None

    # Три основных метода, receive_json (), connect () и disconnect (), используют методы уровня канала
    # group_send(st:72, 77, 85), group_add(st:70) и group_discard(st:80) для управления связью и синхронизацией
    # между всеми различными экземплярами-потребителями одна чат-комната.
    async def connect(self):
        """
        connect(), - генерирует имя комнаты, которое будет использоваться в качестве имени канала
        для вызовов group_*(). После этого нам нужно убедиться, что у пользователя есть разрешение
        находиться здесь, а для этого требуется доступ к базе данных.
        """
        self.order_id = self.scope['url_route']['kwargs']['order_id']
        self.room_group_name = ('customer-service_%s' % self.order_id)
        authorized = False
        if self.scope['user'].is_anonymous:
            await self.close()

        # Для доступа к базе данных от асинхронного потребителя требуется заключить код в синхронную функцию,
        # а затем использовать метод database_sync_to_async().
        # Это потому, что сам Django, особенно ORM, написан синхронно.
        user_type = await database_sync_to_async(self.get_user_type)(self.scope['user'], self.order_id)

        if user_type == ChatConsumer.EMPLOYEE:
            logger.info('Opening chat stream for employee %s', self.scope['user'],)
            authorized = True
        elif user_type == ChatConsumer.CLIENT:
            logger.info('Opening chat stream for client %s', self.scope['user'],)
            authorized = True
        else:
            logger.info('Unauthorized connection from %s', self.scope['user'],)
            await self.close()

        if authorized:
            self.r_conn = await aioredis.create_redis(settings.REDIS_URL)
            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
            await self.accept()
            # В методе connect() мы используем метод group_send()
            # для создания сообщений о присоединении различных пользователей.
            await self.channel_layer.group_send(self.room_group_name, {'type': 'chat_join',
                                                                       'username': self.scope['user'].get_full_name(),},)

    async def disconnect(self, close_code):
        if not self.scope['user'].is_anonymous:
            # В методе disconnect() мы используем метод group_send()
            # для создания сообщений о выходе различных пользователей.
            await self.channel_layer.group_send(self.room_group_name, {'type': 'chat_leave',
                                                                       'username': self.scope['user'].get_full_name(),},)
            logger.info('Closing chat stream for user %s', self.scope['user'],)
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive_json(self, content):
        """
        В методе receive_json() мы используем метод group_send() для обработки двух типов сообщений,
        перечисленных в разделе формата протокола WebSocket. Сообщения типа «message» пересылаются
        всем подключенным потребителям как есть, а сообщения типа «heartbeat» используются для обновления
        времени истечения срока действия ключа Redis (или создания ключа, если он еще не существует).
        """
        typ = content.get('type')
        if typ == 'message':
            await self.channel_layer.group_send(self.room_group_name, {'type': 'chat_message',
                                                                       'username': self.scope['user'].get_full_name(),
                                                                       'message': content['message'],},)
        elif typ == 'heartbeat':
            await self.r_conn.setex('%s_%s' % (self.room_group_name, self.scope['user'].email,), 10, '1') # выдох (через 10 секунд)

# group_send () не отправляет данные обратно в соединение браузера WebSocket.
# Он используется только для передачи информации между потребителями с
# использованием настроенного канального уровня. Каждый потребитель получит
# эти данные через обработчики сообщений.

    # Наконец, в ChatConsumer есть три обработчика: chat_message (), chat_join () и chat_leave ().
    # Все они отправляют сообщение прямо обратно в соединение браузера WebSocket,
    # потому что вся обработка будет происходить во внешнем интерфейсе.

    # Имя обработчика сообщения, который будет обрабатывать сообщение, выводится из поля типа.
    # Если group_send() вызывается с сообщением ['type'] для chat_message,
    # получатель обработает это с помощью обработчика chat_message()
    async def chat_message(self, event):
        await self.send_json(event)

    async def chat_join(self, event):
        await self.send_json(event)

    async def chat_leave(self, event):
        await self.send_json(event)


# События, отправленные сервером (SSE), по сути, представляют собой HTTP-соединение, которое остается открытым и
# продолжает получать порции информации, как только они происходят. Каждый фрагмент информации начинается со слова
# «data:» и заканчивается двумя символами новой строки.
class ChatNotifyConsumer(AsyncHttpConsumer):
    """
    Этот потребитель реализует интерфейс AsyncHttpConsumer с помощью методов handle () и disconnect ().
    Из-за потокового характера конечной точки, которую мы пытаемся построить, нам нужно держать соединение открытым.

    Мы будем вызывать метод send_headers() для запуска HTTP-ответа, и мы будем вызывать send_body() с аргументом
    more_body, установленным в True, в отдельной асинхронной задаче, которая остается активной.
    """
    def is_employee_func(self, user):
        return not user.is_anonymous and user.is_employee

    async def handle(self, body):
        """
        Трансляция осуществляется только для авторизованных пользователей.
        Для неавторизованных пользователей метод handle() вызовет исключение
        StopConsumer, которое остановит потребителя и закроет текущее соединение с клиентом.
        """
        is_employee = await database_sync_to_async(self.is_employee_func)(self.scope['user'])
        if is_employee:
            logger.info("Opening notify stream for user %s and params %s",
                        self.scope.get('user'),
                        self.scope.get('query_string'),)

            # Мы будем вызывать метод send_headers() для запуска HTTP-ответа.
            await self.send_headers(headers=[('Cache-Control', 'no-cache'),
                                             ('Content-Type', 'text/event-stream'),
                                             ('Transfer-Encoding', 'chunked'),])
            self.is_streaming = True
            self.no_poll = (self.scope.get('query_string') == 'nopoll')
            asyncio.get_event_loop().create_task(self.stream())
        else:
            logger.info('Unauthorized notify stream for user %s and params %s',
                        self.scope.get('user'),
                        self.scope.get('query_string'))
            raise StopConsumer('Unauthorized')

    async def stream(self):
        """
        Активация метода stream() добавляется в цикл обработки событий и будет оставаться активной до тех пор,
        пока не будет вызван метод disconnect() (отключение инициировано клиентом). Когда этот метод вызывается,
        он устанавливает для флага is_streaming значение False, что приведет к завершению внутреннего цикла stream(),
        выполняемого в другой задаче asyncio. Метод stream() будет периодически читать из Redis ключи, срок действия
        которых не истек, и отправлять их обратно клиенту. Если во время соединения передан флаг nopoll,
        он выйдет из цикла, не дожидаясь отключения клиента.
        """
        r_conn = await aioredis.create_redis(settings.REDIS_URL)
        while self.is_streaming:
            active_chats = await r_conn.keys('customer-service_*')
            presences = {}
            for i in active_chats:
                _, order_id, user_email = i.decode("utf8").split("_")
                if order_id in presences:
                    presences[order_id].append(user_email)
                else:
                    presences[order_id] = [user_email]
            data = []
            for order_id, emails in presences.items():
                data.append({
                    'link': reverse('cs_chat', kwargs={'order_id': order_id}),
                    'text': '%s (%s)' % (order_id, ", ".join(emails)),
                })
                payload = "data: %s\n\n" % json.dumps(data)
                logger.info('Broadcasting presence info to user %s', self.scope['user'],)
                if self.no_poll:
                    await self.send_body(payload.encode("utf-8"))
                    self.is_streaming = False
                else:
                    await self.send_body(payload.encode("utf-8"), more_body=self.is_streaming,)
                    await asyncio.sleep(5)

    async def disconnect(self):
        logger.info('Closing notify stream for user %s', self.scope.get('user'),)
        self.is_streaming = False


# Этот потребитель полностью асинхронен, за исключением запросов к базе данных.
# Он принимает запрос с идентификатором заказа, указанным в URL-адресе,
# а затем пересылает обратно клиенту результат query_remote_server ().
class OrderTrackerConsumer(AsyncHttpConsumer):
    def verify_user(self, user, order_id):
        order = get_object_or_404(models.Order, pk=order_id)
        return order.user == user

    async def query_remote_server(self, order_id):
        """
        query_remote_server() использует библиотеку, aiohttp,
        для отправки запроса GET на удаленный Pastebin.
        """
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://pastebin.com/raw/Zu8MBGW3"
            ) as resp:
                return await resp.read()

    async def handle(self, body):
        self.order_id = self.scope["url_route"]["kwargs"]["order_id"]
        is_authorized = await database_sync_to_async(self.verify_user)(self.scope["user"], self.order_id)

        if is_authorized:
            logger.info("Order tracking request for user %s and order %s",
                        self.scope.get("user"),
                        self.order_id,)
            payload = await self.query_remote_server(self.order_id)
            logger.info("Order tracking response %s for user %s and order %s",
                        payload,
                        self.scope.get("user"),
                        self.order_id)
            await self.send_response(200, payload)
        else:
            raise StopConsumer("unauthorized")
