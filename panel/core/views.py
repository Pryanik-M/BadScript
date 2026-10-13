"""
Представления — Dashboard и управление устройством.
"""
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import Device
from .forms import DeviceForm


@login_required
def dashboard(request):
    """Главная страница — список всех устройств."""
    devices = Device.objects.all()
    form = DeviceForm()
    return render(request, 'dashboard.html', {
        'devices': devices,
        'form': form,
    })


@login_required
def device_detail(request, pk):
    """Страница управления устройством."""
    device = get_object_or_404(Device, pk=pk)
    return render(request, 'device.html', {
        'device': device,
    })


@login_required
def device_add(request):
    """Добавление нового устройства (POST)."""
    if request.method != 'POST':
        return redirect('dashboard')
    form = DeviceForm(request.POST)
    if form.is_valid():
        device = form.save()
        messages.success(request, f'Устройство "{device.name}" добавлено. UID: {device.uid}')
    else:
        messages.error(request, 'Ошибка при добавлении устройства.')
    return redirect('dashboard')


@login_required
def device_delete(request, pk):
    """Удаление устройства (POST)."""
    if request.method != 'POST':
        return redirect('dashboard')
    device = get_object_or_404(Device, pk=pk)
    name = device.name
    device.delete()
    messages.success(request, f'Устройство "{name}" удалено.')
    return redirect('dashboard')
