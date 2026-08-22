"""Postgres integration tests for the costing module (migration 0009 + round-trip).

Gated on EPIPROC_PG_TEST_DSN exactly like tests/test_integration_postgres.py: with
no DSN set they skip, so `pytest` on a DB-less dev box still passes. Imports are
lazy (inside the fixture) so collection never fails when psycopg or a DB is absent.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def migrated_db():
    """Empty schema -> run every migration -> open the pool (self-contained)."""
    dsn = os.environ.get("EPIPROC_PG_TEST_DSN")
    if not dsn:
        pytest.skip("set EPIPROC_PG_TEST_DSN to run the Postgres integration tests")

    import psycopg

    from epiproc.db import pool as poolmod
    from epiproc.settings import settings

    settings.pg_dsn = dsn
    poolmod.close_pool()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    applied = poolmod.run_migrations()
    poolmod.init_pool()
    try:
        yield {"applied": applied}
    finally:
        poolmod.close_pool()


def test_0009_created_tables_and_seeds(migrated_db):
    assert "0009_costing.sql" in migrated_db["applied"]
    from epiproc.db import costing as db

    boxes = db.list_box_types()
    # 0009 seeds exactly the 4 'Бокс' models — the operator confirmed
    # (2026-08-19) that no other box models exist.
    assert {b["code"] for b in boxes} == {"Бокс 34см", "Бокс 40см", "Бокс 48см", "Бокс 80см"}

    items = db.list_menu_items()
    assert len(items) == 10
    kinds = {i["kind"] for i in items}
    assert kinds == {"packaging_per_unit", "packaging_per_case", "equipment", "labour"}

    # Seed defaults row present with the expected fractions.
    defaults = db.get_costing_defaults()
    assert defaults["vat_rate"] == 0.20
    assert defaults["our_target_margin"] == 0.10


def test_defaults_round_trip(migrated_db):
    from epiproc.db import costing as db

    db.set_costing_defaults({"vat_rate": 0.19, "eur_rate": 1.05, "our_target_margin": 0.12})
    got = db.get_costing_defaults()
    assert got["vat_rate"] == 0.19
    assert got["eur_rate"] == 1.05
    assert got["our_target_margin"] == 0.12
    # Unspecified keys keep their defaults (merge over the fallback).
    assert got["usd_rate"] == 1.14


def test_product_upsert_by_ean(migrated_db):
    from epiproc.db import costing as db

    pid = db.upsert_product(name="Roses Red", ean="5010000000001",
                            units_per_tray=12, selling_price=3.98, retail_price=5.99)
    assert isinstance(pid, int)
    # Same EAN -> update, not a second row.
    pid2 = db.upsert_product(name="Roses Red (renamed)", ean="5010000000001",
                             units_per_tray=10)
    assert pid2 == pid
    got = db.get_product(pid)
    assert got["name"] == "Roses Red (renamed)"
    assert got["units_per_tray"] == 10


def test_save_costing_round_trip_and_recompute(migrated_db):
    """Snapshot invariant: inputs saved == inputs loaded, and recomputing from the
    loaded inputs reproduces the stored results exactly."""
    from epiproc.costing.calc import (
        BoxSelection,
        CostingInputs,
        InboundTransport,
        MaterialLine,
        MenuSelection,
        Outbound,
        compute,
    )
    from epiproc.db import costing as db

    pid = db.upsert_product(name="Lily White", ean="5010000000002",
                            units_per_tray=12, selling_price=3.98, retail_price=5.99)

    inp = CostingInputs(
        units_per_tray=12,
        eur_rate=1.0,
        materials=[MaterialLine(name="Roses", unit_cost=2.45, qty=1)],
        inbound=InboundTransport(qty_per_box=1, boxes_per_cc=252, pallet_rate=125),
        waste_pct=0.01,
        packaging=[MenuSelection(name="Consumables", unit_cost=0.03, qty=1,
                                 kind="packaging_per_case")],
        box=BoxSelection(code="Бокс 34см", price=0.648),
        equipment=[MenuSelection(name="Chep Pallets", unit_cost=10, qty=0,
                                 kind="equipment", divide_by_pack=True)],
        outbound=Outbound(price_per_pallet=50, fill_rate=0.8, boxes_on_order=24),
        labour=[MenuSelection(name="Pack on line", unit_cost=0.10, qty=1, kind="labour")],
        intake_labour_pct=0.10, additional_pct=0.10, vat_rate=0.20,
        retail_price=5.99, selling_price=3.98,
        customer_target_margin=0.35, our_target_margin=0.10,
    )
    results = compute(inp).as_floats()
    saved = db.save_costing(pid, inp.model_dump(), results, created_by="tester", status="final")
    assert saved["version"] == 1

    # A second save bumps the version.
    saved2 = db.save_costing(pid, inp.model_dump(), results, created_by="tester", status="draft")
    assert saved2["version"] == 2

    loaded = db.get_costing(saved["id"])
    assert loaded["inputs"] == inp.model_dump()          # snapshot in == snapshot out
    assert loaded["results"] == results

    # Recompute from the loaded snapshot -> identical results.
    recomputed = compute(CostingInputs(**loaded["inputs"])).as_floats()
    for k, v in results.items():
        if v is None:
            assert recomputed[k] is None
        else:
            assert abs(recomputed[k] - v) < 1e-9

    # Latest final is version 1 (version 2 is a draft).
    latest_final = db.get_latest_costing(pid, status="final")
    assert latest_final["version"] == 1
    assert db.get_latest_costing(pid)["version"] == 2

    # Dashboard read model surfaces the final costing's headline cost.
    dash = db.get_costing_dashboard_data()
    row = next(r for r in dash["products"] if r["id"] == pid)
    assert abs(row["total_direct_cost"] - results["total_direct_cost"]) < 1e-9
    # Our price is derived: total direct cost + 10% (the operator's rule).
    assert row["our_price"] == round(results["total_direct_cost"] * 1.10, 4)
