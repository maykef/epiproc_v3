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
    """Returns a row only when (supplier, filename) is a genuinely owned invoice."""

    def __init__(self, owned):
        self._owned = owned  # set of (supplier, filename) that exist

    def execute(self, sql, params=()):
        supplier, filename = params
        return _Cursor((1,) if (supplier, filename) in self._owned else None)


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
    _patch_pool(monkeypatch, owned={("supplier_b", "secret.pdf")})
    user = {"suppliers": ["supplier_a"], "role": "viewer"}
    # Attack: name supplier_b's file under the allowed supplier_a.
    with pytest.raises(HTTPException) as exc:
        dashboard.serve_pdf("supplier_a", "secret.pdf", user)
    assert exc.value.status_code == 404  # no (supplier_a, secret.pdf) invoice -> denied


def test_disallowed_supplier_is_forbidden(monkeypatch):
    _patch_pool(monkeypatch, owned={("supplier_b", "secret.pdf")})
    user = {"suppliers": ["supplier_a"], "role": "viewer"}
    with pytest.raises(HTTPException) as exc:
        dashboard.serve_pdf("supplier_b", "secret.pdf", user)
    assert exc.value.status_code == 403


def test_owned_file_is_served(monkeypatch, tmp_path):
    # A legitimate download: supplier_a owns the file and the bytes exist on disk.
    _patch_pool(monkeypatch, owned={("supplier_a", "mine.pdf")})
    inv_dir = tmp_path / "invoices"
    (inv_dir / "supplier_a").mkdir(parents=True)
    (inv_dir / "supplier_a" / "mine.pdf").write_bytes(b"%PDF-1.4 ok")
    monkeypatch.setattr(dashboard, "_INVOICES_DIR", inv_dir)
    user = {"suppliers": ["supplier_a"], "role": "viewer"}
    resp = dashboard.serve_pdf("supplier_a", "mine.pdf", user)
    assert resp.status_code == 200
    assert resp.media_type == "application/pdf"
