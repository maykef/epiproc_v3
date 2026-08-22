from __future__ import annotations

import os
from decimal import Decimal

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


def _make_offer_row(
    name: str,
    ean: str,
    material_cost: Decimal,
    row_number: int = 1,
    upt: int = 12,
    box_height_cm: int = 40,
) -> "OfferRow":
    from epiproc.costing.offer_import import OfferRow

    return OfferRow(
        row_number=row_number,
        name=name,
        ean=ean,
        upt=upt,
        material_cost=material_cost,
        box_height_cm=box_height_cm,
        warnings=[],
    )


def _run_import(rows, actor="test", source="test.xlsx"):
    from epiproc.costing.offer_import import run_offer_import

    return run_offer_import(rows, actor=actor, source=source)


def _get_product(name: str, ean: str):
    from epiproc.db import costing as db

    return db.find_product_by_key(name, ean)


def test_import_creates_product_with_auto_price_origin(migrated_db):
    from epiproc.db import costing as db

    name = "Auto Product 1"
    ean = "1000000000001"
    material_cost = Decimal("1.25")
    row = _make_offer_row(name, ean, material_cost)
    report = _run_import([row])

    product = _get_product(name, ean)
    assert product is not None
    assert product["price_origin"] == "auto"
    # also verify the costing was created
    assert report.costings_created == 1


def test_admin_upsert_product_sets_human_price_origin(migrated_db):
    from epiproc.db import costing as db

    name = "Human Product 1"
    ean = "1000000000002"
    product_id = db.upsert_product(name, ean=ean)
    product = _get_product(name, ean)
    assert product["price_origin"] == "human"


def test_reimport_moves_auto_product_prices(migrated_db):
    from epiproc.db import costing as db

    name = "Auto Product 2"
    ean = "1000000000003"
    material_cost_1 = Decimal("1.00")
    material_cost_2 = Decimal("2.50")

    # First import
    row1 = _make_offer_row(name, ean, material_cost_1)
    report1 = _run_import([row1])
    product1 = _get_product(name, ean)
    assert product1["price_origin"] == "auto"
    orig_selling = product1["selling_price"]
    orig_retail = product1["retail_price"]

    # Second import with higher material cost
    row2 = _make_offer_row(name, ean, material_cost_2)
    report2 = _run_import([row2])
    product2 = _get_product(name, ean)
    assert product2["price_origin"] == "auto"
    assert product2["selling_price"] > orig_selling
    assert product2["retail_price"] > orig_retail


def test_reimport_does_not_touch_human_product_prices(migrated_db):
    from epiproc.db import costing as db

    name = "Human Product 2"
    ean = "1000000000004"
    # Create via admin
    db.upsert_product(name, ean=ean, selling_price=Decimal("5.00"), retail_price=Decimal("6.00"))
    product_before = _get_product(name, ean)
    assert product_before["price_origin"] == "human"
    orig_selling = product_before["selling_price"]
    orig_retail = product_before["retail_price"]

    # Import with different material cost
    row = _make_offer_row(name, ean, Decimal("9.99"))
    _run_import([row])

    product_after = _get_product(name, ean)
    assert product_after["selling_price"] == orig_selling
    assert product_after["retail_price"] == orig_retail


def test_costing_from_import_has_offer_import_id(migrated_db):
    from epiproc.db import costing as db
    from epiproc.db.pool import pool

    name = "Auto Product 3"
    ean = "1000000000005"
    row = _make_offer_row(name, ean, Decimal("1.50"))
    report = _run_import([row])
    batch_id = report.batch_id

    product = _get_product(name, ean)
    product_id = product["id"]
    # Get the latest costing for this product via raw SQL
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT offer_import_id FROM costings WHERE product_id = %s ORDER BY version DESC LIMIT 1",
            (product_id,),
        ).fetchone()
    assert row is not None
    assert row["offer_import_id"] == batch_id


def test_hand_saved_costing_has_null_offer_import_id(migrated_db):
    from epiproc.db import costing as db
    from epiproc.db.pool import pool

    name = "Human Product 3"
    ean = "1000000000006"
    product_id = db.upsert_product(name, ean=ean)
    # Save a costing without offer_import_id; use JSON-native types (floats)
    result = db.save_costing(
        product_id,
        inputs={"material_cost": 1.0},
        results={"selling_price": 2.0},
        created_by="admin",
    )
    # save_costing returns only id/version; query the row to check offer_import_id
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT offer_import_id FROM costings WHERE id = %s", (result["id"],)
        ).fetchone()
    assert row is not None
    assert row["offer_import_id"] is None


def test_get_offer_history_newest_first_with_counts(migrated_db):
    from epiproc.db import costing as db

    # Create two imports
    name1 = "Hist Product 1"
    ean1 = "1000000000007"
    name2 = "Hist Product 2"
    ean2 = "1000000000008"

    row1 = _make_offer_row(name1, ean1, Decimal("1.00"))
    report1 = _run_import([row1])
    row2 = _make_offer_row(name2, ean2, Decimal("2.00"))
    report2 = _run_import([row2])

    history = db.get_offer_history()
    assert len(history) >= 2
    # Newest first: the second import should be first
    assert history[0]["id"] == report2.batch_id
    assert history[1]["id"] == report1.batch_id
    # Check costing_count
    assert history[0]["costing_count"] == 1
    assert history[1]["costing_count"] == 1


def test_get_offer_history_data_nests_items(migrated_db):
    from epiproc.db import costing as db

    name = "Nested Product"
    ean = "1000000000009"
    row = _make_offer_row(name, ean, Decimal("3.00"))
    report = _run_import([row])
    batch_id = report.batch_id

    data = db.get_offer_history_data()
    offers = data["offers"]
    # Find our offer
    offer = next(o for o in offers if o["id"] == batch_id)
    assert len(offer["items"]) == 1
    item = offer["items"][0]
    assert item["name"] == name
    assert item["ean"] == ean


def test_failing_import_rolls_back_completely(migrated_db, monkeypatch):
    from epiproc.db import costing as db
    from epiproc.db import costing as costing_db
    from epiproc.db.pool import pool

    name = "Rollback Product"
    ean = "1000000000010"
    row = _make_offer_row(name, ean, Decimal("1.00"))
    src = "rollback_probe_offer.xlsx"

    # Force failure inside the loop
    def boom(*args, **kwargs):
        raise RuntimeError("forced failure")

    monkeypatch.setattr(costing_db, "save_costing", boom)

    with pytest.raises(RuntimeError):
        _run_import([row], source=src)

    # No offer_imports row for this source
    with pool().connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS count FROM offer_imports WHERE filename = %s", (src,)
        )
        assert cur.fetchone()["count"] == 0

        # No costings for this product (if product exists)
        product = _get_product(name, ean)
        if product is not None:
            cur = conn.execute(
                "SELECT COUNT(*) AS count FROM costings WHERE product_id = %s",
                (product["id"],),
            )
            assert cur.fetchone()["count"] == 0

        # No product either? The import might have created the product before failing? Actually it fails before upsert, so no product.
        cur = conn.execute("SELECT COUNT(*) AS count FROM products WHERE name = %s", (name,))
        assert cur.fetchone()["count"] == 0