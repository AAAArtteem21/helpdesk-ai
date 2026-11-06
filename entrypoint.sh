#!/bin/sh
set -eu

python manage.py migrate
python manage.py seed_kb

echo "Starting Mock Helpdesk..."
python mock_helpdesk/server.py &
MOCK_PID=$!

echo "Starting Worker + Sender..."
python manage.py run_worker &
WORKER_PID=$!

cleanup() {
    echo "Stopping background processes..."
    kill -TERM "$WORKER_PID" "$MOCK_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

echo "Starting Gunicorn..."
gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT:-10000}" \
    --workers "${WEB_CONCURRENCY:-1}"
