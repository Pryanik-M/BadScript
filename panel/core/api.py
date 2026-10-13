"""
REST API — отправка команд устройствам.
"""
import json
import logging

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from .models import Device, CommandLog

log = logging.getLogger('api')


@login_required
@require_GET
def device_list(request):
    """GET /api/devices/ — список устройств в JSON."""
    devices = Device.objects.all().values(
        'id', 'uid', 'name', 'is_online', 'last_ip',
        'hostname', 'os_info', 'last_seen',
    )
    data = []
    for d in devices:
        d['last_seen'] = d['last_seen'].isoformat() if d['last_seen'] else None
        data.append(d)
    return JsonResponse({'devices': data})


@login_required
@require_POST
def send_command(request, pk):
    """POST /api/device/<pk>/command/ — отправить команду агенту."""
    device = get_object_or_404(Device, pk=pk)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Неверный JSON'}, status=400)

    cmd_type = body.get('type', '')
    payload = body.get('payload', {})

    if not cmd_type:
        return JsonResponse({'error': 'Не указан тип команды'}, status=400)

    command = {'type': cmd_type, 'payload': payload}

    # Логируем команду
    CommandLog.objects.create(
        device=device,
        command_type=cmd_type,
        payload=payload,
        user=request.user,
    )

    # Отправляем агенту через channel layer
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f'agent_{device.uid}',
        {
            'type': 'send.command',
            'command': command,
        }
    )

    return JsonResponse({'status': 'sent', 'command': cmd_type})
