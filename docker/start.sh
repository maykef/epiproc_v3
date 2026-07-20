#!/usr/bin/env bash
# Entry point: run migrations ONCE up front (before web+worker start, so the
# worker can't race the schema), then launch both processes.
set -euo pipefail

echo "[start] running migrations..."
python -c "from epiproc.db.pool import run_migrations; print('[start] migrations applied:', run_migrations())"

# Always bind the fixed container port 5001. EPIPROC_PORT is the *host* publish
# port (compose maps ${EPIPROC_PORT}:5001) and must NOT be used to bind uvicorn,
# or the published port and the HEALTHCHECK (both 5001) would point at nothing.
python -m uvicorn epiproc.web.app:app --host 0.0.0.0 --port 5001 &
WEB=$!
python -m epiproc.worker &
WORKER=$!

# If either process dies, take the container down so compose restarts it.
wait -n "$WEB" "$WORKER"
exit $?
