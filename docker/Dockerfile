# EpiProc Atlas — the data-free engine image. One image; each customer is a
# container that mounts its own /data and talks to its own Postgres sidecar.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libgomp1 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONPATH=/app EPIPROC_DATA_DIR=/data PYTHONUNBUFFERED=1

# Engine source (templates + migrations + configs travel inside the tree).
COPY pyproject.toml README.md ./
COPY epiproc/ ./epiproc/
COPY configs/ ./configs/
COPY cli.py ./

# Install the project itself so runtime dependencies come from pyproject.toml
# (single source of truth — no hand-maintained pip list to drift). The code also
# stays importable from /app via PYTHONPATH, which is what the app actually runs.
RUN pip install --no-cache-dir .

COPY docker/start.sh ./start.sh
RUN chmod +x ./start.sh

EXPOSE 5001
HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=40s \
    CMD curl -fsS http://localhost:5001/health || exit 1
CMD ["./start.sh"]
