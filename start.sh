#!/bin/sh
# Web process start script (Railway / any PaaS). Runs migrations and setup,
# then serves on the platform-provided $PORT (default 8000).
set -e
python manage.py migrate --noinput
python manage.py setup_roles
python manage.py setup_accounts
python manage.py setup_schedules
python manage.py collectstatic --noinput
exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-3} --timeout 120
