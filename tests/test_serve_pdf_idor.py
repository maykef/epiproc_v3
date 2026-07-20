"""Defect #1 — IDOR in the PDF download route.

A user permitted only Supplier A must not be able to fetch Supplier B's PDF by
naming it under their own supplier. The fix anchors the download to a real
invoice owned by the requested supplier (a DB check) before the basename
fallback can locate the bytes. These tests drive the real `serve_pdf` with a
fake connection so no database is required.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from epiproc.web.routers import dashboard


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Conn:
    """Returns a row only when (supplier, filename) is a genuinely owned invoice.

    `owned` maps (supplier, filename) -> stored path (or None for a legacy row
    with no path yet), mirroring the `SELECT path FROM invoices ...` query.
    """

    def __init__(self, owned):
        self._owned = owned

    def execute(self, sql, params=()):
        supplier, filename = params
        key = (supplier, filename)
        if key in self._owned:
            return _Cursor({"path": self._owned[key]})
        return _Cursor(None)


class _PoolCtx:
    def __init__(self, owned):
        self._owned = owned

    def connection(self):
        conn = _Conn(self._owned)

        class _CM:
            def __enter__(self_):
                return conn

            def __exit__(self_, *a):
                return False

        return _CM()


def _patch_pool(monkeypatch, owned):
    import epiproc.db.pool as poolmod
    monkeypatch.setattr(poolmod, "pool", lambda: _PoolCtx(owned))


def test_cross_supplier_filename_is_rejected(monkeypatch):
    # supplier_b owns "secret.pdf"; the attacker is allowed only supplier_a.
    _patch_pool(monkeypatch, owned={("supplier_b", "secret.pdf"): "/data/invoices/inbox/secret.pdf"})
    user = {"suppliers": ["supplier_a"], "role": "viewer"}
    # Attack: name supplier_b's file under the allowed supplier_a.
    with pytest.raises(HTTPException) as exc:
        dashboard.serve_pdf("supplier_a", "secret.pdf", user)
    assert exc.value.status_code == 404  # no (supplier_a, secret.pdf) invoice -> denied


def test_disallowed_supplier_is_forbidden(monkeypatch):
    _patch_pool(monkeypatch, owned={("supplier_b", "secret.pdf"): None})
    user = {"suppliers": ["supplier_a"], "role": "viewer"}
    with pytest.raises(HTTPException) as exc:
        dashboard.serve_pdf("supplier_b", "secret.pdf", user)
    assert exc.value.status_code == 403


def test_served_via_stored_path_without_walk(monkeypatch, tmp_path):
    # Fast path: the row's stored path points straight at the file (no folder walk).
    import pathlib
    inv_dir = tmp_path / "invoices"
    (inv_dir / "inbox").mkdir(parents=True)
    pdf = inv_dir / "inbox" / "mine.pdf"
    pdf.write_bytes(b"%PDF-1.4 ok")
    monkeypatch.setattr(dashboard, "_INVOICES_DIR", inv_dir)
    _patch_pool(monkeypatch, owned={("supplier_a", "mine.pdf"): str(pdf)})

    # If serve_pdf falls through to the walk, fail loudly — the stored path should
    # be used directly.
    def _no_walk(*a, **k):
        raise AssertionError("serve_pdf walked the tree despite a stored path")
    monkeypatch.setattr(pathlib.Path, "rglob", _no_walk)

    resp = dashboard.serve_pdf("supplier_a", "mine.pdf", {"suppliers": ["supplier_a"], "role": "viewer"})
    assert resp.status_code == 200
    assert resp.media_type == "application/pdf"


def test_legacy_row_without_path_falls_back(monkeypatch, tmp_path):
    # A row with no stored path (pre-backfill) still resolves via the fallback.
    inv_dir = tmp_path / "invoices"
    (inv_dir / "inbox").mkdir(parents=True)
    (inv_dir / "inbox" / "mine.pdf").write_bytes(b"%PDF-1.4 ok")
    monkeypatch.setattr(dashboard, "_INVOICES_DIR", inv_dir)
    _patch_pool(monkeypatch, owned={("supplier_a", "mine.pdf"): None})
    resp = dashboard.serve_pdf("supplier_a", "mine.pdf", {"suppliers": ["supplier_a"], "role": "viewer"})
    assert resp.status_code == 200
