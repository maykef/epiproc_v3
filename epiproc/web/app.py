"""Full FastAPI app for the EpiProc v3 engine web plane.

Ported from v1 dashboard_app/api/main.py. Single-DB: one container == one
customer == one Postgres. On startup it runs migrations and opens the pool; on
shutdown it closes the pool. The middleware stack (security headers, CSRF,
audit, metrics, CORS) and routers are the v1 set minus the unauthenticated JSON
data routers and reports/services/search (out of scope for this port).
"""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from epiproc import __version__
from epiproc.db.pool import close_pool, init_pool, run_migrations
from epiproc.settings import settings
from epiproc.web.metrics import MetricsMiddleware, metrics_response
from epiproc.web.routers import admin, dashboard, login, reset, usage
from epiproc.web.routers import health as health_router
from epiproc.web.security import (
    AuditMiddleware,
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    limiter,
)
from epiproc.web.session import get_session_user


@asynccontextmanager
async def lifespan(app: FastAPI):
    applied = run_migrations()
    if applied:
        print("[web] migrations applied:", applied)
    init_pool()
    yield
    close_pool()


app = FastAPI(
    title="EpiProc v3 engine",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# Rate limiter state
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Middleware stack (bottom-to-top execution: security headers → CSRF → audit → metrics → CORS)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(AuditMiddleware)
app.add_middleware(MetricsMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5001", "http://127.0.0.1:5001"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Routers
app.include_router(login.router)
app.include_router(reset.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(usage.router)
app.include_router(health_router.router)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok", "version": __version__, "institution": settings.institution}


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    # Never open by default — the gauges expose supplier names and per-supplier
    # invoice counts. With EPIPROC_METRICS_TOKEN set, Prometheus authenticates with a
    # bearer token; otherwise access requires an admin session.
    token = settings.metrics_token
    if token:
        auth = request.headers.get("authorization", "")
        supplied = auth[7:] if auth[:7].lower() == "bearer " else request.headers.get("x-metrics-token", "")
        if not (supplied and secrets.compare_digest(supplied, token)):
            return Response("unauthorized", status_code=401)
    else:
        user = get_session_user(request)  # 307 -> /login when unauthenticated
        if user.get("role") != "admin":
            return Response("admin only", status_code=403)
    return metrics_response()
