#!/bin/sh
# Container entrypoint: run DB migrations for the web app, then exec the CMD.
# Migrations run only when DATABASE_URL is set AND we're starting the web server
# (so the Celery worker container doesn't also race to migrate).
set -e

if [ -n "$DATABASE_URL" ] && echo "$*" | grep -q "uvicorn"; then
  echo "→ Applying database migrations (alembic upgrade head)…"
  alembic upgrade head
fi

exec "$@"
