#!/usr/bin/env sh
set -eu

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  python manage.py migrate --noinput
fi

if [ "${ENABLE_DEMO_DATABASE_CONNECTION:-true}" = "true" ]; then
  python manage.py ensure_demo_database
fi

exec "$@"
