#!/bin/sh
set -eu

if [ "${QTABLE_RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "QTable: applying database migrations"
  alembic upgrade head
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 9000
