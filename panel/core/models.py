"""
Модели — устройства и лог команд.
"""
from django.db import models
from django.contrib.auth.models import User


class Device(models.Model):
    """Удалённое устройство (агент)."""
    uid = models.CharField('UID', max_length=32, unique=True, db_index=True)
    name = models.CharField('Название', max_length=128)
    secret = models.CharField('Токен', max_length=256)
    is_online = models.BooleanField('Онлайн', default=False)
    last_seen = models.DateTimeField('Последнее соединение', null=True, blank=True)
    last_ip = models.CharField('IP', max_length=45, blank=True, default='')
    os_info = models.CharField('ОС', max_length=256, blank=True, default='')
    hostname = models.CharField('Hostname', max_length=128, blank=True, default='')
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        ordering = ['-is_online', '-last_seen']
        verbose_name = 'Устройство'
        verbose_name_plural = 'Устройства'

    def __str__(self):
        icon = '\U0001f7e2' if self.is_online else '\U0001f534'
        return f'{icon} {self.name} ({self.uid[:8]})'


class CommandLog(models.Model):
    """Лог отправленных команд."""
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='commands')
    command_type = models.CharField('Тип', max_length=64)
    payload = models.JSONField('Данные', default=dict, blank=True)
    result = models.JSONField('Результат', default=dict, blank=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField('Время', auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Команда'
        verbose_name_plural = 'Команды'

    def __str__(self):
        return f'{self.device.name}: {self.command_type}'
