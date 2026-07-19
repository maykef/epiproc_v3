# EpiProc v3 — the data-free engine image. One image; each customer is a
# container that mounts its own /data and talks to its own Postgres sidecar.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libgomp1 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONPATH=/app EPIPROC_DATA_DIR=/data PYTHONUNBUFFERED=1

# Dependencies (explicit — no editable package install, so data files just live in the tree)
RUN pip install --no-cache-dir \
        "fastapi>=0.110" "uvicorn[standard]>=0.29" \
        "psycopg[binary]>=3.1" "psycopg-pool>=3.2" \
        "pydantic>=2" "pydantic-settings>=2" "jinja2>=3.1" "pyyaml>=6" \
        "python-multipart>=0.0.9" "argon2-cffi>=23" "pyotp>=2.9" "qrcode[pil]>=8" \
        "openai>=1.30" "anthropic>=0.40" "pymupdf>=1.24" "pillow>=10" \
        "prometheus-client>=0.20" "slowapi>=0.1.9"

# Engine source (templates + migrations travel inside epiproc/)
COPY epiproc/ ./epiproc/
COPY configs/ ./configs/
COPY cli.py ./
COPY docker/start.sh ./start.sh
RUN chmod +x ./start.sh

EXPOSE 5001
HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=40s \
    CMD curl -fsS http://localhost:5001/health || exit 1
CMD ["./start.sh"]
