"""
Формы — добавление устройства.
"""
import uuid
from django import forms
from .models import Device


class DeviceForm(forms.ModelForm):
    """Форма добавления нового устройства."""

    class Meta:
        model = Device
        fields = ['name', 'secret']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Имя устройства',
            }),
            'secret': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Секретный токен',
            }),
        }

    def save(self, commit=True):
        instance = super().save(commit=False)
        if not instance.uid:
            instance.uid = uuid.uuid4().hex[:8]
        if commit:
            instance.save()
        return instance
