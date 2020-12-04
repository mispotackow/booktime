from io import BytesIO
import logging
from PIL import Image
from django.conf import settings
from django.core.files.base import ContentFile
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from rest_framework.authtoken.models import Token
from .models import ProductImage, Basket, OrderLine, Order

THUMBNAIL_SIZE = (300, 300)

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=ProductImage)
def generate_thumbnail(sender, instance, **kwargs):
    logger.info(
        "Создание миниатюры для продукта %d",
        instance.product.id,
    )
    image = Image.open(instance.image)
    image = image.convert("RGB")
    image.thumbnail(THUMBNAIL_SIZE, Image.ANTIALIAS)

    temp_thumb = BytesIO()
    image.save(temp_thumb, "JPEG")
    temp_thumb.seek(0)

    # установите save=False, иначе он будет работать в бесконечном цикле
    instance.thumbnail.save(
        instance.image.name,
        ContentFile(temp_thumb.read()),
        save=False,
    )
    temp_thumb.close()


@receiver(user_logged_in)
def merge_baskets_if_found(sender, user, request, **kwargs):
    # request - Объект, значение атрибута которого требуется получить.
    # basket - Имя атрибута, значение которого требуется получить.
    # None - Значение по умолчанию, которое будет возвращено, если объект не располагает указанным атрибутом.
    anonymous_basket = getattr(request, 'basket', None)
    if anonymous_basket:
        try:
            loggedin_basket = Basket.objects.get(user=user, status=Basket.OPEN)
            for line in anonymous_basket.basketline_set.all():
                line.basket = loggedin_basket
                line.save()
            anonymous_basket.delete()
            request.basket = loggedin_basket
            logger.info('Merged basket to id %d', loggedin_basket.id)
        except Basket.DoesNotExist:
            anonymous_basket.uset = user
            anonymous_basket.save()
            logger.info('Assigned user to basket id %d', anonymous_basket.id,)


# Отмеченные заказы больше не отображаются в api списка.
# чтобы не зависеть от того, как они помечены, через REST API или через администратора Django.
@receiver(post_save, sender=OrderLine)
def orderline_to_order_status(sender, instance, **kwargs):
    """
    Этот сигнал будет выполнен после сохранения экземпляров модели OrderLine.
    Первое, что он делает, это проверяет, имеют ли какие-либо строки заказа,
    связанные с заказом, статусы ниже «sent». Если есть, выполнение прекращается.
    Если под статусом «sent» нет строчки, весь заказ помечается как «done».
    """
    if not instance.order.lines.filter(status__lt=OrderLine.SENT).exists():
        logger.info("All lines for order %d have been processed."
                    "Marking as done.", instance.order.id,)
        instance.order.status = Order.DONE
        instance.order.save()


# С этого момента каждый новый пользователь может получить доступ к аутентифицированным
# конечным точкам DRF с помощью токенов, помимо уже существующих методов.
@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_auth_token(
        sender, instance=None, created=False, **kwargs
):
    if created:
        Token.objects.create(user=instance)
