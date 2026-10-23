#!/usr/bin/env bash
# Starts Postgres and Redis without Docker (for environments where Docker isn't available)
# and bootstraps roles. With Docker, use `docker compose -f deploy/local/docker-compose.yml up`.
set -euo pipefail
PGDATA="${PGDATA:-/var/lib/re-pg/data}"
PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"
if ! pg_isready -h localhost -p 5432 >/dev/null 2>&1; then
  if [ ! -d "$PGDATA" ]; then
    mkdir -p "$(dirname "$PGDATA")" && chown postgres "$(dirname "$PGDATA")"
    su postgres -c "$PGBIN/initdb -D $PGDATA -U postgres --auth=trust" >/dev/null
  fi
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-p 5432 -k /tmp' -l $(dirname "$PGDATA")/log start"
fi
redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes --port 6379 >/dev/null
psql -h localhost -U postgres -v ON_ERROR_STOP=1 -f "$(dirname "$0")/db-bootstrap.sql" >/dev/null
echo "postgres + redis ready"
