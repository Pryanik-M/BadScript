"""
ASGI конфигурация — HTTP + WebSocket маршрутизация.
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'panel.settings')
django.setup()

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler
from core.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    'http': ASGIStaticFilesHandler(get_asgi_application()),
    'websocket': URLRouter(websocket_urlpatterns),
})
