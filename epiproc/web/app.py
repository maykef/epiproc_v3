"""Minimal bootable FastAPI app for the skeleton.

Boots, runs migrations, exposes /health, and renders the EMPTY dashboard so the
engine-in-a-box milestone (fresh container -> login -> empty dashboard) can be
demonstrated. Auth/admin/full-dashboard routers are ported in the next step.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from epiproc import __version__
from epiproc.db.pool import close_pool, init_pool, run_migrations
from epiproc.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    applied = run_migrations()
    if applied:
        print("[web] migrations applied:", applied)
    init_pool()
    yield
    close_pool()


app = FastAPI(title="EpiProc v3 engine", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__, "institution": settings.institution})


@app.get("/health/ready")
def ready() -> JSONResponse:
    try:
        with init_pool().connection() as conn:
            conn.execute("SELECT 1")
        return JSONResponse({"status": "ready"})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"status": "degraded", "error": str(e)}, status_code=503)


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    # Placeholder until the full dashboard router is ported. The real template
    # (dashboard_template.html) is already in web/templates/, rendered empty.
    return (
        f"<h1>EpiProc v3 — {settings.institution}</h1>"
        "<p>Engine online. No data yet. Drop invoices and run <code>process</code>.</p>"
    )
