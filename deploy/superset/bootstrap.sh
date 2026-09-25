#!/bin/bash
set -e
superset db upgrade
superset fab create-admin --username "${SUPERSET_ADMIN_USER:-admin}" --firstname Admin --lastname User \
  --email admin@superset.local --password "${SUPERSET_ADMIN_PASSWORD:-admin}" || true
superset init
exec gunicorn --bind 0.0.0.0:8088 --workers 2 --timeout 120 "superset.app:create_app()"
