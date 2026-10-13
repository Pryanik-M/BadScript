"""
WebSocket consumers — relay между агентами и браузером.

Поток данных:
  Агент <--encrypted bytes--> AgentConsumer <--channel layer--> BrowserConsumer <--JSON--> Браузер
"""
import json
import hashlib
import base64
import logging
from datetime import datetime

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger('consumers')


# ---------------------------------------------------------------------------
# Утилиты шифрования (совместимы с shared.py агента)
# ---------------------------------------------------------------------------
def _make_fernet(secret: str) -> Fernet:
    """Создаёт Fernet-ключ из секрета (совместимо с derive_key_simple)."""
    raw = hashlib.sha256(secret.encode()).digest()
    key = base64.urlsafe_b64encode(raw[:32])
    return Fernet(key)


def _encrypt(fernet: Fernet, obj: dict) -> bytes:
    return fernet.encrypt(json.dumps(obj).encode())


def _decrypt(fernet: Fernet, data: bytes) -> dict:
    return json.loads(fernet.decrypt(data))


# ---------------------------------------------------------------------------
# Реестр активных агентов: uid -> channel_name
# ---------------------------------------------------------------------------
_agent_channels: dict[str, str] = {}
_agent_fernets: dict[str, Fernet] = {}


class AgentConsumer(AsyncWebsocketConsumer):
    """
    Принимает WebSocket-соединения от агентов.
    Агент шлёт зашифрованные Fernet-сообщения (bytes).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.device = None
        self.fernet = None
        self.authenticated = False
        self.device_uid = None

    async def connect(self):
        await self.accept()
        log.info('Агент подключился, ожидаем аутентификацию...')

    async def disconnect(self, code):
        if self.device_uid:
            # Убираем из реестра
            _agent_channels.pop(self.device_uid, None)
            _agent_fernets.pop(self.device_uid, None)
            # Помечаем офлайн
            await self._set_device_offline()
            # Уведомляем браузер
            await self.channel_layer.group_send(
                f'browser_{self.device_uid}',
                {
                    'type': 'agent.response',
                    'data': {'type': 'connection_status', 'payload': {'agent_online': False}},
                }
            )
            # Покидаем группу
            await self.channel_layer.group_discard(
                f'agent_{self.device_uid}', self.channel_name
            )
            log.info('Агент %s отключился', self.device_uid)

    async def receive(self, text_data=None, bytes_data=None):
        """
        Агент может слать:
        1) Текстовый JSON (первое сообщение — auth)
        2) Зашифрованные bytes (после auth)
        """
        if not self.authenticated:
            await self._handle_auth(text_data, bytes_data)
            return

        # Расшифровываем сообщение от агента
        if bytes_data:
            try:
                msg = _decrypt(self.fernet, bytes_data)
            except (InvalidToken, Exception) as e:
                log.warning('Ошибка расшифровки от агента %s: %s', self.device_uid, e)
                return
        elif text_data:
            try:
                msg = json.loads(text_data)
            except json.JSONDecodeError:
                return
        else:
            return

        # Пересылаем ответ агента в группу браузеров
        await self.channel_layer.group_send(
            f'browser_{self.device_uid}',
            {
                'type': 'agent.response',
                'data': msg,
            }
        )

    async def _handle_auth(self, text_data, bytes_data):
        """Обработка аутентификации агента."""
        auth_msg = None

        # Агент может послать auth как текст или как зашифрованные байты
        if text_data:
            try:
                auth_msg = json.loads(text_data)
            except json.JSONDecodeError:
                await self.close(code=4001)
                return
        elif bytes_data:
            # Попробуем расшифровать — но нам нужен секрет из auth_msg...
            # Значит первое сообщение должно быть текстовым JSON
            await self.send(text_data=json.dumps({
                'type': 'auth_fail',
                'payload': {'reason': 'send_auth_as_text'},
            }))
            return

        if not auth_msg or auth_msg.get('type') != 'auth':
            await self.send(text_data=json.dumps({
                'type': 'auth_fail',
                'payload': {'reason': 'expected_auth_message'},
            }))
            await self.close(code=4001)
            return

        uid = auth_msg.get('uid', '')
        token = auth_msg.get('token', '')
        hostname = auth_msg.get('hostname', '')
        os_info = auth_msg.get('os_info', '')

        # Ищем устройство в БД
        device = await self._get_device(uid)
        if not device:
            await self.send(text_data=json.dumps({
                'type': 'auth_fail',
                'payload': {'reason': 'device_not_found'},
            }))
            await self.close(code=4002)
            return

        # Проверяем токен
        if device.secret != token:
            await self.send(text_data=json.dumps({
                'type': 'auth_fail',
                'payload': {'reason': 'invalid_token'},
            }))
            await self.close(code=4003)
            return

        # Аутентификация успешна
        self.device = device
        self.device_uid = device.uid
        self.fernet = _make_fernet(token)
        self.authenticated = True

        # Сохраняем в реестр
        _agent_channels[device.uid] = self.channel_name
        _agent_fernets[device.uid] = self.fernet

        # Обновляем статус в БД
        client_ip = self.scope.get('client', ['', ''])[0]
        await self._set_device_online(client_ip, hostname, os_info)

        # Присоединяемся к группе
        await self.channel_layer.group_add(
            f'agent_{device.uid}', self.channel_name
        )

        # Отправляем подтверждение
        await self.send(text_data=json.dumps({
            'type': 'auth_ok',
            'payload': {},
        }))

        # Уведомляем браузер
        await self.channel_layer.group_send(
            f'browser_{device.uid}',
            {
                'type': 'agent.response',
                'data': {'type': 'connection_status', 'payload': {'agent_online': True}},
            }
        )

        log.info('Агент аутентифицирован: %s (%s) IP=%s', device.name, device.uid, client_ip)

    # --- Обработчик команд от BrowserConsumer через channel layer ---
    async def send_command(self, event):
        """Получаем команду от браузера, шифруем и отправляем агенту."""
        command = event['command']
        encrypted = _encrypt(self.fernet, command)
        await self.send(bytes_data=encrypted)

    # --- DB helpers ---
    @database_sync_to_async
    def _get_device(self, uid):
        from core.models import Device
        try:
            return Device.objects.get(uid=uid)
        except Device.DoesNotExist:
            return None

    @database_sync_to_async
    def _set_device_online(self, ip, hostname, os_info):
        from core.models import Device
        from django.utils import timezone
        Device.objects.filter(uid=self.device_uid).update(
            is_online=True,
            last_seen=timezone.now(),
            last_ip=ip or '',
            hostname=hostname or self.device.hostname,
            os_info=os_info or self.device.os_info,
        )

    @database_sync_to_async
    def _set_device_offline(self):
        from core.models import Device
        from django.utils import timezone
        Device.objects.filter(uid=self.device_uid).update(
            is_online=False,
            last_seen=timezone.now(),
        )


class BrowserConsumer(AsyncWebsocketConsumer):
    """
    WebSocket для браузера — управление конкретным устройством.
    Данные ходят в виде чистого JSON (без шифрования).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.device = None
        self.device_uid = None

    async def connect(self):
        device_id = self.scope['url_route']['kwargs']['device_id']

        # Загружаем устройство
        self.device = await self._get_device_by_id(device_id)
        if not self.device:
            await self.close(code=4004)
            return

        self.device_uid = self.device.uid

        # Присоединяемся к группе браузеров этого устройства
        await self.channel_layer.group_add(
            f'browser_{self.device_uid}', self.channel_name
        )
        await self.accept()
        log.info('Браузер подключился к устройству %s', self.device.name)

        # Сообщаем статус подключения агента
        is_agent_online = self.device_uid in _agent_channels
        await self.send(text_data=json.dumps({
            'type': 'connection_status',
            'payload': {'agent_online': is_agent_online},
        }))

    async def disconnect(self, code):
        if self.device_uid:
            await self.channel_layer.group_discard(
                f'browser_{self.device_uid}', self.channel_name
            )

    async def receive(self, text_data=None, bytes_data=None):
        """Браузер отправляет команду — пересылаем агенту."""
        if not text_data:
            return

        try:
            command = json.loads(text_data)
        except json.JSONDecodeError:
            return

        if not self.device_uid:
            return

        # Проверяем, подключён ли агент
        if self.device_uid not in _agent_channels:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'payload': {'message': 'Агент не подключён'},
            }))
            return

        # Отправляем команду агенту через channel layer
        await self.channel_layer.group_send(
            f'agent_{self.device_uid}',
            {
                'type': 'send.command',
                'command': command,
            }
        )

    # --- Обработчик ответов от AgentConsumer через channel layer ---
    async def agent_response(self, event):
        """Получаем ответ от агента, пересылаем в браузер как JSON."""
        data = event['data']
        await self.send(text_data=json.dumps(data))

    # --- DB helpers ---
    @database_sync_to_async
    def _get_device_by_id(self, device_id):
        from core.models import Device
        try:
            return Device.objects.get(pk=device_id)
        except Device.DoesNotExist:
            return None
