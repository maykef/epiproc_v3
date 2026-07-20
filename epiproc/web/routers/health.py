"""Health check endpoints.

GET /health/ready    — readiness: checks Postgres and disk space
GET /health/detailed — operational detail: per-supplier counts, queue depth, active sessions

Ported from v1. Single-DB: no tenant schema — invoices live in the default
schema; readiness uses the pooled connection / settings.pg_dsn.
"""
from __future__ import annotations

import shutil
import urllib.request
from typing import Any
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from epiproc.db.pool import pool
from epiproc.settings import settings
from epiproc.web.metrics import set_active_sessions, set_invoices_total, set_vllm_healthy
from epiproc.web.session import get_session_user

router = APIRouter(tags=["health"])


def _vllm_metrics_url() -> str:
    """vLLM's Prometheus endpoint, derived from the configured base URL.

    Hardcoding 127.0.0.1 was wrong: inside the container the shared GPU server is
    reached at settings.vllm_url's host (host.docker.internal), not loopback, so the
    probe was always false. vLLM serves /metrics at the root, not under /v1.
    """
    parts = urlsplit(settings.vllm_url)
    return f"{parts.scheme}://{parts.netloc}/metrics"


_DISK_WARN_PCT = 85.0


def _check_postgres() -> dict[str, Any]:
    try:
        with psycopg.connect(settings.pg_dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _check_vllm() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(_vllm_metrics_url(), timeout=2) as resp:
            healthy = resp.status == 200
    except Exception:
        healthy = False
    set_vllm_healthy(healthy)
    return {"ok": healthy}


def _check_disk() -> dict[str, Any]:
    usage = shutil.disk_usage("/")
    pct = usage.used / usage.total * 100
    return {
        "ok": pct < _DISK_WARN_PCT,
        "used_pct": round(pct, 1),
        "free_gb": round(usage.free / 1_073_741_824, 1),
    }


@router.get("/health/ready")
def health_ready():
    """Readiness probe — checked by the reverse proxy and Prometheus."""
    pg = _check_postgres()
    disk = _check_disk()
    vllm = _check_vllm()

    ok = pg["ok"] and disk["ok"]  # vLLM may legitimately be offline
    status = 200 if ok else 503
    return JSONResponse(
        status_code=status,
        content={
            "status": "ok" if ok else "degraded",
            "postgres": pg,
            "disk": disk,
            "vllm": vllm,
        },
    )


@router.get("/health/detailed")
def health_detailed(request: Request):
    """Detailed operational snapshot — for the operator dashboard.

    Admin-only: this exposes supplier names, per-supplier invoice counts, job-queue
    depth and active-session counts — business metadata that must not be readable
    without authentication on a product that sells data confidentiality. The plain
    /health and /health/ready probes stay open for the reverse proxy / Prometheus.
    """
    user = get_session_user(request)  # raises 307 -> /login when unauthenticated
    if user.get("role") != "admin":
        return JSONResponse(status_code=403, content={"error": "admin only"})
    try:
        with pool().connection() as conn:
            # Per-supplier invoice counts (default schema)
            supplier_rows = conn.execute(
                """
                SELECT supplier,
                       COUNT(*) FILTER (WHERE extraction_error IS NULL) AS clean,
                       COUNT(*) AS total,
                       MAX(created_at) AS last_processed
                FROM invoices
                GROUP BY supplier
                ORDER BY supplier
                """
            ).fetchall()

            # Job queue depth
            queue_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM jobs
                GROUP BY status
                """
            ).fetchall()

            # Active sessions (users logged in within the last 24 hours)
            session_row = conn.execute(
                """
                SELECT COUNT(DISTINCT username) AS n
                FROM audit_log
                WHERE action = 'login'
                  AND ts > now() - interval '24 hours'
                """
            ).fetchone()

    except Exception as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})

    # Update gauges while we have fresh data
    active = session_row["n"] if session_row else 0
    set_active_sessions(active)
    for row in supplier_rows:
        set_invoices_total(row["supplier"], row["clean"])

    suppliers = [
        {
            "supplier": r["supplier"],
            "clean_invoices": r["clean"],
            "total_invoices": r["total"],
            "last_processed": r["last_processed"].isoformat() if r["last_processed"] else None,
        }
        for r in supplier_rows
    ]
    queue = {r["status"]: r["n"] for r in queue_rows}

    return {
        "suppliers": suppliers,
        "job_queue": queue,
        "active_sessions_24h": active,
        "disk": _check_disk(),
    }
