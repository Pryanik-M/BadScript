"""
WSGI конфигурация (для продакшена за nginx/gunicorn).
"""
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'panel.settings')
application = get_wsgi_application()
