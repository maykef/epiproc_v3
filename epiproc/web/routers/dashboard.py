"""Serve full-page HTML dashboards directly.

Ported from v1 dashboard_app/api/routers/dashboard.py. Single-DB: the supplier
list comes from epiproc.db.dashboard.get_suppliers() (DB-first, config
fallback); the pre-built standalone route is dropped.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from epiproc.db.dashboard import get_suppliers
from epiproc.settings import settings
from epiproc.web.dashboard_html import build_dashboard_html, build_multi_dashboard_html
from epiproc.web.security import RATE_DASHBOARD, limiter
from epiproc.web.session import get_session_user

_INVOICES_DIR = Path(settings.data_dir) / "invoices"

router = APIRouter(include_in_schema=False)


def _available(user: dict) -> list[str]:
    allowed = user.get("suppliers", [])
    return [s for s in get_suppliers() if not allowed or s in allowed]


@router.get("/dashboard", response_class=HTMLResponse)
@limiter.limit(RATE_DASHBOARD)
def dashboard_index(
    request: Request,
    current_user: Annotated[dict, Depends(get_session_user)],
    v: str = Query(default=""),
):
    """Unified multi-supplier dashboard — one page, supplier dropdown in nav."""
    import time
    if not v:
        return RedirectResponse(url=f"/dashboard?v={int(time.time())}", status_code=302)
    available = _available(current_user)
    if not available:
        raise HTTPException(status_code=403, detail="No suppliers available.")
    csrf_token = getattr(request.state, "csrf_token", "")
    return HTMLResponse(
        build_multi_dashboard_html(available, is_admin=current_user.get("role") == "admin", csrf_token=csrf_token),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/dashboard/{supplier}", response_class=HTMLResponse)
@limiter.limit(RATE_DASHBOARD)
def dashboard_supplier(
    request: Request,
    supplier: str,
    current_user: Annotated[dict, Depends(get_session_user)],
):
    """Single-supplier view — kept for backward compatibility."""
    allowed = current_user.get("suppliers", [])
    if allowed and supplier not in allowed:
        raise HTTPException(status_code=403, detail=f"No access to '{supplier}'.")
    if supplier not in get_suppliers():
        raise HTTPException(status_code=404, detail=f"Supplier '{supplier}' not found.")
    csrf_token = getattr(request.state, "csrf_token", "")
    return HTMLResponse(
        build_dashboard_html(supplier, is_admin=current_user.get("role") == "admin", csrf_token=csrf_token),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/pdf/{supplier}/{filename}")
def serve_pdf(
    supplier: str,
    filename: str,
    current_user: Annotated[dict, Depends(get_session_user)],
):
    allowed = current_user.get("suppliers", [])
    if allowed and supplier not in allowed:
        raise HTTPException(status_code=403, detail=f"No access to '{supplier}'.")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if "/" in supplier or "\\" in supplier or ".." in supplier:
        raise HTTPException(status_code=400, detail="Invalid supplier.")
    # Authorisation anchor + direct path: look up the invoice owned by THIS
    # supplier and read its stored file path in one query. The `allowed` check
    # above only guards the URL's supplier segment; requiring a matching invoice
    # row is what stops the basename fallback below serving ANY supplier's PDF to
    # anyone who can name it (IDOR).
    from epiproc.db.pool import pool
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT path FROM invoices WHERE supplier = %s AND filename = %s LIMIT 1",
            (supplier, filename),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found.")

    # Fast path: the row stores the file's location, so open it directly — no walk.
    # Verify it still resolves under invoices/ (defence against a bad path value).
    stored = row.get("path")
    if stored:
        p = Path(stored)
        try:
            resolved = p.resolve()
            if p.is_file() and _INVOICES_DIR.resolve() in resolved.parents:
                return FileResponse(str(p), media_type="application/pdf")
        except OSError:
            pass

    # Fallback for legacy rows without a stored path (backfill_paths fills these on
    # boot) or a moved file. Scope the basename search to THIS supplier's own folder:
    # a global rglob could serve a *different* supplier's identically-named PDF even
    # though the authz row check passed — leaking one customer-supplier's document to
    # a user entitled only to another. Post-backfill the stored-path fast path above
    # wins; this only runs for un-backfilled or moved files.
    supplier_dir = _INVOICES_DIR / supplier
    pdf_path = supplier_dir / filename
    if not pdf_path.exists() and supplier_dir.is_dir():
        match = next((p for p in supplier_dir.rglob("*.pdf") if p.name == filename), None)
        if match is not None:
            pdf_path = match
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(str(pdf_path), media_type="application/pdf")
