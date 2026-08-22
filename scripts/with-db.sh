#!/usr/bin/env bash
# Run a command with DATABASE_URL pointed at the in-cluster Postgres.
#
# kubectl port-forward drops connections fairly readily on this setup, and a
# dropped forward mid-migration looks exactly like a migration failure. This
# starts a fresh forward, waits for it, runs the command, and always tears the
# forward down afterwards.
#
#   scripts/with-db.sh uv run alembic upgrade head
set -euo pipefail

NS=${NS:-meetings}
LOCAL_PORT=${LOCAL_PORT:-55432}
CTX=${KCTX:-kind-meetings}

password=$(kubectl --context "$CTX" -n "$NS" get secret meetings-postgres-app \
  -o jsonpath='{.data.password}' | base64 -d)

kubectl --context "$CTX" -n "$NS" port-forward svc/meetings-postgres-rw \
  "${LOCAL_PORT}:5432" >/tmp/with-db-pf.log 2>&1 &
pf_pid=$!
trap 'kill "$pf_pid" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  nc -z 127.0.0.1 "$LOCAL_PORT" 2>/dev/null && break
  sleep 0.5
done

export DATABASE_URL="postgresql+asyncpg://appuser:${password}@127.0.0.1:${LOCAL_PORT}/meetings"
"$@"
