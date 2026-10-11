#!/bin/sh
set -eu

mode="${1:-web}"

if [ "$mode" = "web" ] || [ "$mode" = "worker" ]; then
  echo "Applicazione migrazioni Alembic..."
  alembic upgrade head
fi

if [ "$mode" = "web" ]; then
  exec python -m gbrain.cli web
elif [ "$mode" = "worker" ]; then
  exec python -m gbrain.worker
elif [ "$mode" = "migrate" ]; then
  exec alembic upgrade head
else
  exec "$@"
fi
