#!/usr/bin/env bash
# Entry point: migrations run on app startup (lifespan); launch web + worker.
set -euo pipefail

python -m uvicorn epiproc.web.app:app --host 0.0.0.0 --port "${EPIPROC_PORT:-5001}" &
WEB=$!
python -m epiproc.worker &
WORKER=$!

# If either process dies, take the container down so compose restarts it.
wait -n "$WEB" "$WORKER"
exit $?
