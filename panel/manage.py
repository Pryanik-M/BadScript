#!/usr/bin/env python
"""Django manage.py"""
import os
import sys

def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'panel.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django не установлен. Установите: pip install django"
        ) from exc
    execute_from_command_line(sys.argv)

if __name__ == '__main__':
    main()
