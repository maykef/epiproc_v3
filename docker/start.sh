#!/usr/bin/env bash
# Entry point: run migrations ONCE up front (before web+worker start, so the
# worker can't race the schema), then launch both processes.
set -euo pipefail

echo "[start] running migrations..."
python -c "from epiproc.db.pool import run_migrations; print('[start] migrations applied:', run_migrations())"

python -m uvicorn epiproc.web.app:app --host 0.0.0.0 --port "${EPIPROC_PORT:-5001}" &
WEB=$!
python -m epiproc.worker &
WORKER=$!

# If either process dies, take the container down so compose restarts it.
wait -n "$WEB" "$WORKER"
exit $?
