#!/bin/sh
# Web process start script (Railway / any PaaS). Runs migrations and setup,
# then serves on the platform-provided $PORT (default 8000).
set -e
python manage.py migrate --noinput
# First admin login: set DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD
# (optionally DJANGO_SUPERUSER_EMAIL) in the platform variables; the user is
# created once and skipped on later starts. setup_roles then gives it the
# Admin role.
if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
  python manage.py shell -c "
from django.contrib.auth.models import User
import os
u = os.environ['DJANGO_SUPERUSER_USERNAME']
if User.objects.filter(username=u).exists():
    print(f'superuser {u}: exists')
else:
    User.objects.create_superuser(u, os.environ.get('DJANGO_SUPERUSER_EMAIL', ''), os.environ['DJANGO_SUPERUSER_PASSWORD'])
    print(f'superuser {u}: created')
"
fi
python manage.py setup_roles
python manage.py setup_accounts
python manage.py setup_schedules
python manage.py collectstatic --noinput
exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-3} --timeout 120
