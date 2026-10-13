"""
URL маршруты панели.
"""
from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views
from core import views, api

urlpatterns = [
    path('admin/', admin.site.urls),

    # Авторизация
    path('login/', auth_views.LoginView.as_view(template_name='login.html'), name='login'),
    path('login', auth_views.LoginView.as_view(template_name='login.html')),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('logout', auth_views.LogoutView.as_view()),

    # Страницы
    path('', views.dashboard, name='dashboard'),
    path('device/<int:pk>/', views.device_detail, name='device_detail'),

    # Управление устройствами
    path('api/device/add/', views.device_add, name='device_add'),
    path('api/device/<int:pk>/delete/', views.device_delete, name='device_delete'),

    # API
    path('api/devices/', api.device_list, name='api_device_list'),
    path('api/device/<int:pk>/command/', api.send_command, name='api_send_command'),
]
