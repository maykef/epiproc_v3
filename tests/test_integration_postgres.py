"""Integration tests against a REAL Postgres — the layer the fakes can't cover.

These exercise the migration runner (never run in CI before) and a full
insert_record -> dashboard-query round-trip through psycopg, catching the class of
SQL/migration regression that has reached the live instance in the past.

Gated on EPIPROC_PG_TEST_DSN: with no DSN set they skip, so `pytest` on a dev box
with no database still passes. CI sets the DSN to a `postgres:` service container.
Imports are lazy (inside the fixture) so collection never fails when psycopg or a
DB is absent.
"""
from __future__ import annotations

import os
import pathlib

import pytest

pytestmark = pytest.mark.integration

_MIGRATIONS = (
    pathlib.Path(__file__).resolve().parent.parent / "epiproc" / "db" / "migrations"
)


@pytest.fixture(scope="module")
def migrated_db():
    """Reset to an empty schema, run every migration, open the pool.

    Yields the list of migration files applied from empty so a test can assert the
    full 0001->0008 chain applied cleanly. Tears the pool down afterwards.
    """
    dsn = os.environ.get("EPIPROC_PG_TEST_DSN")
    if not dsn:
        pytest.skip("set EPIPROC_PG_TEST_DSN to run the Postgres integration tests")

    import psycopg

    from epiproc.db import pool as poolmod
    from epiproc.settings import settings

    settings.pg_dsn = dsn
    poolmod.close_pool()  # drop any pool bound to a different DSN

    # Deterministic, re-runnable locally: start from a truly empty schema.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")

    applied = poolmod.run_migrations()
    poolmod.init_pool()
    try:
        yield {"applied": applied}
    finally:
        poolmod.close_pool()


def test_all_migrations_apply_from_empty(migrated_db):
    expected = [p.name for p in sorted(_MIGRATIONS.glob("*.sql"))]
    assert expected, "no migration files found"
    # The whole chain applied, in order, on a fresh database.
    assert migrated_db["applied"] == expected


def test_migrations_are_idempotent(migrated_db):
    from epiproc.db.pool import pool, run_migrations

    # Second run applies nothing…
    assert run_migrations() == []
    # …and schema_migrations records exactly the files on disk.
    expected = {p.name for p in _MIGRATIONS.glob("*.sql")}
    with pool().connection() as conn:
        names = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    assert names == expected


def test_insert_record_round_trips_to_dashboard(migrated_db):
    from epiproc.db.dashboard import get_data_quality, get_suppliers
    from epiproc.db.invoices import insert_record
    from epiproc.db.pool import pool

    record = {
        "invoice_number": "INV-42",
        "invoice_date": "2026-01-15",
        "currency": "GBP",
        "document_type": "invoice",
        "seller": {"name": "Acme Supplies"},
        "totals": {"subtotal": 100.0, "total": 100.0},
        "line_items": [
            {"position": 1, "article": "WIDGET-1", "description": "Steel widget",
             "quantity": 40, "unit_price": 2.0, "total_price": 80.0},
            {"position": 2, "article": "GADGET-1", "description": "Copper gadget",
             "quantity": 10, "unit_price": 2.0, "total_price": 20.0},
        ],
    }

    with pool().connection() as conn:
        inv_id = insert_record(conn, "acme_supplies", "inv42.pdf", record, [],
                               status="ok", path="/data/invoices/inv42.pdf")
    assert isinstance(inv_id, int)

    # Real SQL: header + both line items landed.
    with pool().connection() as conn:
        inv_count = conn.execute(
            "SELECT count(*) AS c FROM invoices WHERE supplier=%s", ("acme_supplies",)
        ).fetchone()["c"]
        item_count = conn.execute(
            "SELECT count(*) AS c FROM invoice_items WHERE invoice_id=%s", (inv_id,)
        ).fetchone()["c"]
    assert inv_count == 1
    assert item_count == 2

    # Dashboard aggregation queries run against the real rows.
    assert "acme_supplies" in get_suppliers()
    dq = get_data_quality()
    assert dq["uncategorised_items"] == 2  # insert_record leaves category NULL


def test_insert_record_is_idempotent_on_same_supplier_filename(migrated_db):
    """The (supplier, filename) upsert (delete-then-insert) must not duplicate rows
    when the same file is re-processed — the invariant behind the dedup fall-through."""
    from epiproc.db.invoices import insert_record
    from epiproc.db.pool import pool

    record = {
        "invoice_number": "INV-99",
        "currency": "GBP",
        "seller": {"name": "Re Run Ltd"},
        "totals": {"total": 10.0},
        "line_items": [{"position": 1, "description": "Thing",
                        "quantity": 1, "unit_price": 10.0, "total_price": 10.0}],
    }
    with pool().connection() as conn:
        insert_record(conn, "rerun", "same.pdf", record, [], path="/data/invoices/same.pdf")
        insert_record(conn, "rerun", "same.pdf", record, [], path="/data/invoices/same.pdf")

    with pool().connection() as conn:
        n_inv = conn.execute(
            "SELECT count(*) AS c FROM invoices WHERE supplier=%s AND filename=%s",
            ("rerun", "same.pdf"),
        ).fetchone()["c"]
        n_items = conn.execute(
            "SELECT count(*) AS c FROM invoice_items ii "
            "JOIN invoices i ON ii.invoice_id=i.id WHERE i.supplier=%s AND i.filename=%s",
            ("rerun", "same.pdf"),
        ).fetchone()["c"]
    assert n_inv == 1, "same (supplier, filename) must overwrite, not duplicate"
    assert n_items == 1, "stale line items from the prior insert must be gone"
