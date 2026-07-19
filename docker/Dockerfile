# EpiProc v3 — the data-free engine image. One image; each customer is a
# container that mounts its own /data and talks to its own Postgres sidecar.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libgomp1 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY epiproc/ ./epiproc/
COPY configs/ ./configs/
COPY cli.py ./
COPY docker/start.sh ./start.sh
RUN chmod +x ./start.sh

ENV EPIPROC_DATA_DIR=/data
EXPOSE 5001
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://localhost:5001/health || exit 1
CMD ["./start.sh"]
