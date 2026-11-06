#!/bin/sh
set -e

python manage.py migrate
python manage.py seed_kb

exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:${PORT:-10000} \
    --workers 4