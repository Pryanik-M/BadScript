"""
Регистрация моделей в Django Admin.
"""
from django.contrib import admin
from .models import Device, CommandLog


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ('name', 'uid', 'is_online', 'last_ip', 'hostname', 'os_info', 'last_seen')
    list_filter = ('is_online',)
    search_fields = ('name', 'uid', 'hostname', 'last_ip')
    readonly_fields = ('uid', 'created_at', 'last_seen', 'last_ip', 'is_online')


@admin.register(CommandLog)
class CommandLogAdmin(admin.ModelAdmin):
    list_display = ('device', 'command_type', 'user', 'created_at')
    list_filter = ('command_type', 'device')
    search_fields = ('command_type',)
    readonly_fields = ('created_at',)
