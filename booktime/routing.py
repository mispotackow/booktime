from django.urls import re_path
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.http import AsgiHandler
from .auth import TokenGetAuthMiddlewareStack
import main.routing

application = ProtocolTypeRouter(
    {
        # (http -> Django views добавляется по умолчанию)
        "websocket": TokenGetAuthMiddlewareStack(
            URLRouter(main.routing.websocket_urlpatterns)
        ),
        "http": URLRouter(
            main.routing.http_urlpatterns
            + [re_path(r"", AsgiHandler)]
        ),
    }
)
