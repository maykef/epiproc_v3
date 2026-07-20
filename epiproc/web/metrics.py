"""Prometheus metrics for the EpiProc engine web plane.

Metrics are registered once at import time.  MetricsMiddleware updates
request counters and latency histograms on every response.
"""
from __future__ import annotations

import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ── Metrics definitions ───────────────────────────────────────────────────────

requests_total = Counter(
    "epiproc_requests_total",
    "Total HTTP requests handled",
    ["method", "path", "status"],
)

request_duration = Histogram(
    "epiproc_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

active_sessions = Gauge(
    "epiproc_active_sessions",
    "Number of active authenticated sessions (approximation via recent audit log)",
)

jobs_total = Counter(
    "epiproc_jobs_total",
    "Job queue events by terminal status and type",
    ["status", "type"],
)

extraction_errors_total = Counter(
    "epiproc_extraction_errors_total",
    "Extraction failures logged to invoices.extraction_error",
    ["supplier"],
)

invoices_total = Gauge(
    "epiproc_invoices_total",
    "Clean invoices in Postgres per supplier",
    ["supplier"],
)

vllm_healthy = Gauge(
    "epiproc_vllm_healthy",
    "1 if the vLLM server is reachable, 0 otherwise",
)


# ── Convenience update functions ──────────────────────────────────────────────

def record_job_created(job_type: str) -> None:
    jobs_total.labels(status="queued", type=job_type).inc()


def record_job_finished(job_type: str, status: str) -> None:
    """Call with status='completed' or 'error' when a job reaches a terminal state."""
    jobs_total.labels(status=status, type=job_type).inc()


def record_extraction_error(supplier: str) -> None:
    extraction_errors_total.labels(supplier=supplier).inc()


def set_invoices_total(supplier: str, count: int) -> None:
    invoices_total.labels(supplier=supplier).set(count)


def set_vllm_healthy(healthy: bool) -> None:
    vllm_healthy.set(1 if healthy else 0)


def set_active_sessions(count: int) -> None:
    active_sessions.set(count)


# ── Metrics HTTP endpoint ─────────────────────────────────────────────────────

def metrics_response() -> Response:
    """Return a Prometheus text-format response for GET /metrics."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ── Middleware ────────────────────────────────────────────────────────────────

# Paths that generate high cardinality (UUIDs, IDs) — normalise them
_PATH_SUBS: list[tuple[str, str]] = [
    ("/reports/jobs/", "/reports/jobs/{id}"),
    ("/invoices/",     "/invoices/{id}"),
    ("/admin/users/",  "/admin/users/{id}"),
]


def _normalise_path(path: str) -> str:
    for prefix, replacement in _PATH_SUBS:
        if path.startswith(prefix) and path != prefix:
            return replacement
    return path


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record request count and latency for every response."""

    async def dispatch(self, request: Request, call_next):
        path = _normalise_path(request.url.path)
        method = request.method
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        status = str(response.status_code)
        requests_total.labels(method=method, path=path, status=status).inc()
        request_duration.labels(method=method, path=path).observe(elapsed)
        return response
