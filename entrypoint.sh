#!/bin/sh
set -e

# Bind mounts nem sempre disparam inotify; polling garante reload ao editar app.py.
export WATCHDOG_FORCE_POLLING=true
export WATCHDOG_POLLING_INTERVAL=1

exec watchmedo auto-restart \
  --directory=/app \
  --pattern='*.py' \
  --recursive \
  --debounce-interval=0.5 \
  -- python -u /app/app.py
