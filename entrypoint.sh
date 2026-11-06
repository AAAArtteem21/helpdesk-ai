#!/bin/sh
set -e

case "$SERVICE_TYPE" in
  web)
    python manage.py migrate
    python manage.py seed_kb
    exec gunicorn config.wsgi:application \
      --bind "0.0.0.0:${PORT:-10000}" \
      --workers "${WEB_CONCURRENCY:-1}"
    ;;
  worker)
    exec python manage.py run_worker
    ;;
  mock)
    exec python mock_helpdesk/server.py
    ;;
  *)
    echo "Unknown SERVICE_TYPE: $SERVICE_TYPE" >&2
    exit 1
    ;;
esac