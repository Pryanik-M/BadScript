"""
WebSocket маршруты.
"""
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    # Агенты подключаются сюда (шифрованный канал)
    re_path(r'^ws/agent/$', consumers.AgentConsumer.as_asgi()),
    # Браузер подключается сюда (просмотр конкретного устройства)
    re_path(r'^ws/panel/(?P<device_id>\d+)/$', consumers.BrowserConsumer.as_asgi()),
]
