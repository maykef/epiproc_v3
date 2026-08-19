"""Offer-sheet batch import (phase 2): parser, row->CostingInputs mapping, and
the batch driver.

Unit tests (parser + mapping) run without a DB; the batch-driver tests are
Postgres integration tests gated on EPIPROC_PG_TEST_DSN exactly like
test_costing_integration.py — with no DSN they skip, so `pytest` on a DB-less
dev box still passes.
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from epiproc.costing.calc import (
    BoxSelection,
    CostingInputs,
    InboundTransport,
    MaterialLine,
    MenuSelection,
    Outbound,
    compute,
)
from epiproc.costing.offer_import import OfferRow, build_inputs, parse_offer, run_offer_import

FIXTURE_XLSX = Path(__file__).parent / "fixtures" / "offer_sample.xlsx"

# Mirror of the 0009/0010 seeds, so the mapping tests are independent of the DB.
# The operator confirmed (2026-08-19) that only these four box models exist —
# every offer height must resolve into the smallest one it fits.
DEFAULTS = {
    "vat_rate": 0.20, "eur_rate": 1.0, "usd_rate": 1.14,
    "waste_pct": 0.01, "intake_labour_pct": 0.10, "additional_pct": 0.10,
    "customer_target_margin": 0.35, "our_target_margin": 0.10,
    "pallet_rate": 125, "boxes_per_cc": 252, "qty_per_box": 1,
    "price_per_pallet": 50, "fill_rate": 0.8, "boxes_on_order": 24,
    "fuel_surcharge_pct": 0,
}
BOX_TYPES = [
    {"code": "Бокс 34см", "dimensions": "60х40х34", "price": 0.648},
    {"code": "Бокс 40см", "dimensions": "60х40х40", "price": 0.686},
    {"code": "Бокс 48см", "dimensions": "60х40х48", "price": 0.742},
    {"code": "Бокс 80см", "dimensions": "60х40х80", "price": 0.979},
]
MENU_ITEMS = [
    {"kind": "packaging_per_unit", "name": "Small Sleeve (40cm)", "unit_cost": 0.14},
    {"kind": "packaging_per_unit", "name": "Price Label", "unit_cost": 0.02},
    {"kind": "packaging_per_case", "name": "Consumables", "unit_cost": 0.03},
    {"kind": "packaging_per_case", "name": "Box end", "unit_cost": 0.01},
    {"kind": "packaging_per_case", "name": "Additional label(s)", "unit_cost": 0.005},
    {"kind": "equipment", "name": "Chep Pallets", "unit_cost": 10},
    {"kind": "equipment", "name": "Add ons", "unit_cost": 0},
    {"kind": "labour", "name": "Pack on line", "unit_cost": 0.10},
    {"kind": "labour", "name": "Labelling", "unit_cost": 0.01},
]


# ── Parser ────────────────────────────────────────────────────────────────────


def test_parse_offer_fixture_rows():
    rows = parse_offer(str(FIXTURE_XLSX))
    assert len(rows) == 6

    clean = rows[0]                     # 3: clean row
    assert clean.name == "Roses Red 50cm"
    assert clean.ean == "8711111111118"
    assert clean.upt == 12
    assert clean.material_cost == Decimal("2.1000")
    assert clean.box_height_cm == 48
    assert clean.layers_per_cc == 4
    assert clean.trays_per_layer == 6
    assert clean.per_pallet_raw == "24x16"
    assert clean.warnings == []

    blank_ean = rows[1]                 # 4: blank EAN
    assert blank_ean.name == "Roses White"
    assert blank_ean.ean is None
    assert blank_ean.warnings == []

    value_err = rows[2]                 # 5: #VALUE! EAN -> None, no warning
    assert value_err.name == "Roses Pink"
    assert value_err.ean is None
    assert value_err.material_cost == Decimal("2.3000")

    split_h = rows[3]                   # 6: "20 / 25 cm" -> first integer
    assert split_h.name == "Tulip Mix"
    assert split_h.box_height_cm == 20

    no_upt = rows[4]                    # 7: missing UPT + suspect-length EAN
    assert no_upt.name == "Lily Stargazer"
    assert no_upt.upt is None
    assert no_upt.ean == "12345"        # junk-length digits kept but flagged
    assert "upt_missing" in no_upt.warnings
    assert "ean_suspect" in no_upt.warnings

    no_box = rows[5]                    # 8: height 35 parses; box match fails at mapping
    assert no_box.name == "Peony"
    assert no_box.box_height_cm == 35
    assert no_box.warnings == []


def test_parse_offer_bytes_equals_path():
    from_path = parse_offer(str(FIXTURE_XLSX))
    from_bytes = parse_offer(FIXTURE_XLSX.read_bytes())
    assert [r.model_dump() for r in from_path] == [r.model_dump() for r in from_bytes]


# ── Mapping: OfferRow -> CostingInputs ────────────────────────────────────────


def test_build_inputs_maps_clean_row():
    clean = parse_offer(str(FIXTURE_XLSX))[0]
    inputs = build_inputs(clean, DEFAULTS, BOX_TYPES, MENU_ITEMS)
    assert inputs is not None

    # Per-product values from the sheet: UPT (J), material cost N — not M — and
    # the box resolved by the height from L.
    assert inputs.units_per_tray == 12
    assert inputs.materials == [MaterialLine(name="Roses Red 50cm", unit_cost=2.1, qty=1.0)]
    assert inputs.box == BoxSelection(code="Бокс 48см", price=0.742)

    # Shared values from defaults, never from the sheet.
    assert inputs.inbound == InboundTransport(qty_per_box=1.0, boxes_per_cc=252.0,
                                             pallet_rate=125.0)
    assert inputs.outbound == Outbound(price_per_pallet=50.0, fill_rate=0.8,
                                       boxes_on_order=24.0, fuel_surcharge_pct=0.0)
    assert inputs.eur_rate == 1.0 and inputs.waste_pct == 0.01
    assert inputs.intake_labour_pct == 0.10 and inputs.additional_pct == 0.10
    assert inputs.vat_rate == 0.20
    assert inputs.retail_price is None and inputs.selling_price is None

    # Default menu selection: Consumables / Pack on line / Labelling on, all else off.
    qty = {s.name: s.qty for s in inputs.packaging + inputs.equipment + inputs.labour}
    assert qty["Consumables"] == 1.0
    assert qty["Pack on line"] == 1.0
    assert qty["Labelling"] == 1.0
    assert all(q == 0.0 for n, q in qty.items()
               if n not in ("Consumables", "Pack on line", "Labelling"))
    assert inputs.equipment and all(s.qty == 0.0 for s in inputs.equipment)

    # The mapped inputs compute exactly like the same inputs built by hand.
    expected = CostingInputs(
        units_per_tray=12,
        eur_rate=1.0,
        materials=[MaterialLine(name="Roses Red 50cm", unit_cost=2.1, qty=1)],
        inbound=InboundTransport(qty_per_box=1, boxes_per_cc=252, pallet_rate=125),
        waste_pct=0.01,
        packaging=[MenuSelection(name="Consumables", unit_cost=0.03, qty=1,
                                 kind="packaging_per_case")],
        box=BoxSelection(code="Бокс 48см", price=0.742),
        outbound=Outbound(price_per_pallet=50, fill_rate=0.8, boxes_on_order=24),
        labour=[MenuSelection(name="Pack on line", unit_cost=0.10, qty=1, kind="labour"),
                MenuSelection(name="Labelling", unit_cost=0.01, qty=1, kind="labour")],
        intake_labour_pct=0.10, additional_pct=0.10, vat_rate=0.20,
        customer_target_margin=0.35, our_target_margin=0.10,
    )
    got = compute(inputs).as_floats()["total_direct_cost"]
    want = compute(expected).as_floats()["total_direct_cost"]
    assert got == pytest.approx(want, rel=1e-9)


def test_build_inputs_none_when_uncostable():
    rows = parse_offer(str(FIXTURE_XLSX))

    lily = rows[4]                      # missing UPT
    assert build_inputs(lily, DEFAULTS, BOX_TYPES, MENU_ITEMS) is None

    no_price = OfferRow(row_number=1, name="X", ean=None, upt=10,
                        material_cost=None, box_height_cm=48)
    assert build_inputs(no_price, DEFAULTS, BOX_TYPES, MENU_ITEMS) is None

    no_box = OfferRow(row_number=1, name="Y", ean=None, upt=10,
                      material_cost=Decimal("1.5"), box_height_cm=99)
    assert build_inputs(no_box, DEFAULTS, BOX_TYPES, MENU_ITEMS) is None
    assert "box_unresolved" in no_box.warnings


def test_build_inputs_resolves_fitting_box():
    """Every offer height lands in the smallest of the four real box models
    (34/40/48/80 cm — the operator confirmed no others exist) that fits it."""
    rows = parse_offer(str(FIXTURE_XLSX))

    clean = rows[0]                     # height 48 -> exact model
    inputs = build_inputs(clean, DEFAULTS, BOX_TYPES, MENU_ITEMS)
    assert inputs is not None
    assert inputs.box == BoxSelection(code="Бокс 48см", price=0.742)

    tulip = rows[3]                     # height 20 -> fits in 34
    inputs = build_inputs(tulip, DEFAULTS, BOX_TYPES, MENU_ITEMS)
    assert inputs is not None
    assert inputs.box == BoxSelection(code="Бокс 34см", price=0.648)

    peony = rows[5]                     # height 35 -> fits in 40
    inputs = build_inputs(peony, DEFAULTS, BOX_TYPES, MENU_ITEMS)
    assert inputs is not None
    assert inputs.box == BoxSelection(code="Бокс 40см", price=0.686)


# ── Batch driver (Postgres integration, gated) ────────────────────────────────

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def migrated_db():
    """Empty schema -> run every migration -> open the pool (self-contained),
    mirroring test_costing_integration.py."""
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


def _rows():
    return parse_offer(str(FIXTURE_XLSX))


def test_migration_0010_merges_engine_defaults(migrated_db):
    assert "0010_costing_offer_defaults.sql" in migrated_db["applied"]
    from epiproc.db import costing as db

    defaults = db.get_costing_defaults()
    assert defaults["pallet_rate"] == 125
    assert defaults["boxes_per_cc"] == 252
    assert defaults["qty_per_box"] == 1
    assert defaults["price_per_pallet"] == 50
    assert defaults["fill_rate"] == 0.8
    assert defaults["boxes_on_order"] == 24
    assert defaults["fuel_surcharge_pct"] == 0


def test_migration_0011_batch_table_and_four_models_only(migrated_db):
    assert "0011_costing_offer_imports.sql" in migrated_db["applied"]
    from epiproc.db import costing as db
    from epiproc.db.pool import pool

    # The operator confirmed only four box models exist (34/40/48/80 cm).
    codes = {b["code"] for b in db.list_box_types()}
    assert codes == {"Бокс 34см", "Бокс 40см", "Бокс 48см", "Бокс 80см"}

    with pool().connection() as conn:
        assert conn.execute(
            "SELECT to_regclass('offer_imports') AS t").fetchone()["t"] == "offer_imports"
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'offer_imports'")}
        assert "finalised" in cols   # bulk-finalise checkbox


def test_offer_import_dry_run_writes_nothing(migrated_db):
    from epiproc.db import costing as db
    from epiproc.db.pool import pool

    products_before = db.list_products()
    report = run_offer_import(_rows(), "tester", dry_run=True, source="offer_sample.xlsx")

    assert report.products_created == 6
    assert report.costings_created == 5        # every row with UPT + price + box
    assert report.skipped == 0
    statuses = {r.status for r in report.rows}
    assert statuses == {"costed", "attention"}
    assert sum(1 for r in report.rows if r.status == "attention") == 1   # Lily (no UPT)
    assert db.list_products() == products_before   # read-only
    assert report.batch_id is None
    with pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS c FROM offer_imports").fetchone()["c"] == 0


def test_dry_run_mirrors_duplicate_ean_merge(migrated_db):
    """Two rows sharing one valid EAN merge into a single product in the real
    run — the dry-run preview must report the same created/updated split and
    the same price for the second row (its stored price comes from the first
    row written earlier in the same batch)."""
    from epiproc.db import costing as db

    def twin(n: int) -> OfferRow:
        return OfferRow(row_number=n, name=f"Twin {n}", ean="8710000000001",
                        upt=10, material_cost=Decimal("2.0"), box_height_cm=48)

    rows = [twin(1), twin(2)]
    dry = run_offer_import(rows, "tester", dry_run=True, source="x.xlsx")
    assert dry.products_created == 1
    assert dry.products_updated == 1
    assert [r.product_action for r in dry.rows] == ["created", "updated"]
    assert dry.rows[1].selling_price == dry.rows[0].selling_price
    assert dry.rows[1].retail_price == dry.rows[0].retail_price

    real = run_offer_import(rows, "tester", source="x.xlsx")
    assert real.products_created == 1
    assert real.products_updated == 1
    assert len(db.list_products()) == 1
    assert dry.rows[1].selling_price == real.rows[1].selling_price

    # Leave the shared test DB as this test found it (costings FK has no cascade).
    from epiproc.db.pool import pool
    with pool().connection() as conn:
        pid = conn.execute(
            "SELECT id FROM products WHERE ean = '8710000000001'").fetchone()
        if pid is not None:
            conn.execute("DELETE FROM costings WHERE product_id = %s", (pid["id"],))
            conn.execute("DELETE FROM products WHERE id = %s", (pid["id"],))
        conn.execute("DELETE FROM offer_imports")   # the twin batch row too


def test_offer_import_creates_drafts_and_reruns_are_idempotent(migrated_db):
    from epiproc.db import costing as db
    from epiproc.db.pool import pool

    report = run_offer_import(_rows(), "tester", source="offer_sample.xlsx")
    assert report.products_created == 6
    assert report.products_updated == 0
    assert report.costings_created == 5
    assert report.batch_id is not None

    products = db.list_products()
    assert len(products) == 6

    # Every imported costing is a DRAFT — never auto-finalised.
    for p in products:
        for c in db.list_costings(p["id"]):
            assert c["status"] == "draft"

    roses = db.get_product_by_ean("8711111111118")
    assert roses["units_per_tray"] == 12
    assert len(db.list_costings(roses["id"])) == 1

    # A product with no stored selling price gets the auto-target price
    # (cost ÷ 0.9, rounded to 2dp) and the snapshot records it — so every row
    # shows cost → profit → total with no manual steps.
    with pool().connection() as conn:
        v1 = conn.execute(
            "SELECT inputs, results FROM costings WHERE product_id = %s ORDER BY version",
            (roses["id"],),
        ).fetchone()
    cost = v1["results"]["total_direct_cost"]
    auto = round(cost / 0.9, 2)
    assert roses["selling_price"] == auto
    assert v1["inputs"]["selling_price"] == auto
    assert v1["results"]["our_gp"] == pytest.approx(auto - cost, abs=1e-9)

    # Auto retail comes from the workbook's Target Retail formula
    # (selling ÷ (1 − customer margin) × (1 + VAT), rounded 2dp) so customer
    # GP% fills in with zero manual steps too.
    auto_retail = round(auto / 0.65 * 1.2, 2)
    assert roses["retail_price"] == auto_retail
    assert v1["inputs"]["retail_price"] == auto_retail
    assert v1["results"]["customer_gp_pct"] is not None

    # A short height resolves to the smallest real model that fits.
    tulip_report = next(r for r in report.rows if r.name == "Tulip Mix")
    assert tulip_report.box_code == "Бокс 34см"

    # Attention rows still import as products, just without a costing.
    lily = db.get_product_by_name("Lily Stargazer")
    assert lily is not None and db.list_costings(lily["id"]) == []

    # Missing-UPT row: inserted with the UPT fallback; the junk-length EAN is
    # never persisted (products.ean is UNIQUE — junk must not merge products).
    assert lily["units_per_tray"] == 1
    assert lily["ean"] is None

    # One upload = one batch version, recorded with the file name + counts.
    with pool().connection() as conn:
        batches = conn.execute("SELECT * FROM offer_imports ORDER BY id").fetchall()
    assert len(batches) == 1
    assert batches[0]["filename"] == "offer_sample.xlsx"
    assert batches[0]["uploaded_by"] == "tester"
    assert batches[0]["costings_created"] == 5

    # Re-import: same products updated, NEW draft versions, no duplicates — and
    # an admin-set selling price survives untouched (stored price beats the
    # auto-target, and is what the new snapshot computes against).
    db.upsert_product(name="Roses Red 50cm", ean="8711111111118",
                      selling_price=3.98, retail_price=5.99)
    report2 = run_offer_import(_rows(), "tester", source="offer_sample.xlsx")
    assert report2.products_created == 0
    assert report2.products_updated == 6
    assert report2.costings_created == 5

    assert len(db.list_products()) == 6
    roses2 = db.get_product_by_ean("8711111111118")
    assert roses2["selling_price"] == 3.98
    assert roses2["retail_price"] == 5.99
    costings = db.list_costings(roses2["id"])
    assert len(costings) == 2
    assert {c["version"] for c in costings} == {1, 2}
    assert all(c["status"] == "draft" for c in costings)
    with pool().connection() as conn:
        snapshots = conn.execute(
            "SELECT version, inputs, results FROM costings "
            "WHERE product_id = %s ORDER BY version",
            (roses2["id"],),
        ).fetchall()
    v2 = next(s for s in snapshots if s["version"] == 2)
    assert v2["inputs"]["selling_price"] == 3.98
    # Stored retail also wins over the auto target.
    assert v2["inputs"]["retail_price"] == 5.99
    assert v2["results"]["our_gp"] == pytest.approx(
        3.98 - v2["results"]["total_direct_cost"], abs=1e-9)

    # The second upload is its own batch version.
    with pool().connection() as conn:
        batches = conn.execute("SELECT * FROM offer_imports ORDER BY id").fetchall()
    assert len(batches) == 2
    assert report2.batch_id == batches[1]["id"]


def test_dry_run_flags_duplicate_ean(migrated_db):
    """Two rows sharing a valid EAN would merge into one product — the second
    row must be flagged, not silently overwrite."""
    rows = [
        OfferRow(row_number=1, name="A", ean="8711111111118", upt=10,
                 material_cost=Decimal("1.5000"), box_height_cm=48),
        OfferRow(row_number=2, name="B", ean="8711111111118", upt=10,
                 material_cost=Decimal("1.5000"), box_height_cm=48),
    ]
    report = run_offer_import(rows, "tester", dry_run=True)
    assert "duplicate_ean" not in report.rows[0].warnings
    assert "duplicate_ean" in report.rows[1].warnings


def test_junk_ean_rows_never_merge_or_collide(migrated_db):
    """Two products sharing the same junk-length EAN are distinct name-keyed
    products — the junk EAN is flagged, never persisted, so the UNIQUE
    constraint on products.ean can't collide."""
    from epiproc.db import costing as db

    rows = [
        OfferRow(row_number=1, name="Junk A", ean="12345", upt=10,
                 material_cost=Decimal("1.5000"), box_height_cm=48),
        OfferRow(row_number=2, name="Junk B", ean="12345", upt=10,
                 material_cost=Decimal("1.5000"), box_height_cm=48),
    ]
    report = run_offer_import(rows, "tester")
    assert report.products_created == 2
    a = db.get_product_by_name("Junk A")
    b = db.get_product_by_name("Junk B")
    assert a["id"] != b["id"]
    assert a["ean"] is None and b["ean"] is None
    assert "ean_suspect" in report.rows[0].warnings
    assert "ean_suspect" in report.rows[1].warnings


def test_bulk_finalise_is_opt_in(migrated_db):
    """The wizard's bulk-finalise checkbox saves this batch's costings as FINAL
    (default without it is draft — asserted by the main test above)."""
    from epiproc.db import costing as db
    from epiproc.db.pool import pool

    report = run_offer_import(_rows(), "tester", source="offer_sample.xlsx",
                              finalise=True)
    assert report.costings_created == 5

    # Every product costed by THIS batch has its latest costing final; the
    # attention row got no costing at all (nothing to finalise). Products from
    # earlier tests untouched by this batch keep their own (draft) history.
    for r in report.rows:
        if r.status != "costed":
            continue
        ean = r.ean if (r.ean and len(r.ean) in (8, 13)) else None
        p = db.get_product_by_ean(ean) if ean else db.get_product_by_name(r.name)
        cs = db.list_costings(p["id"])   # version DESC — cs[0] is the latest
        assert cs and cs[0]["status"] == "final"
    with pool().connection() as conn:
        b = conn.execute(
            "SELECT finalised FROM offer_imports ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert b["finalised"] is True
