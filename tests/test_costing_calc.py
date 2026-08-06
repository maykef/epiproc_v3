"""Golden + edge-case tests for the pure costing calc.

The golden case reproduces the source workbook's worked example exactly (see the
task spec / docs/costing.md); every intermediate and every output is asserted to
1e-9 so a formula regression can't hide behind a rounding tolerance. The calc is
pure, so this runs with no database and no EPIPROC_PG_TEST_DSN.
"""
from __future__ import annotations

from epiproc.costing.calc import (
    BoxSelection,
    CostingInputs,
    InboundTransport,
    MaterialLine,
    MenuSelection,
    Outbound,
    PackagingPcts,
    compute,
)

TOL = 1e-9


def _golden_inputs() -> CostingInputs:
    return CostingInputs(
        units_per_tray=12,
        eur_rate=1.0,
        materials=[
            MaterialLine(name="Roses", unit_cost=2.45, qty=1),
            MaterialLine(name="Lily 2", unit_cost=0.0, qty=0),
        ],
        inbound=InboundTransport(qty_per_box=1, boxes_per_cc=252, pallet_rate=125),
        waste_pct=0.01,
        packaging=[
            MenuSelection(name="Small Sleeve (40cm)", unit_cost=0.14, qty=0,
                          kind="packaging_per_unit"),
            MenuSelection(name="Price Label", unit_cost=0.02, qty=0,
                          kind="packaging_per_unit"),
            MenuSelection(name="Consumables", unit_cost=0.03, qty=1,
                          kind="packaging_per_case"),
            MenuSelection(name="Box end", unit_cost=0.01, qty=0,
                          kind="packaging_per_case"),
            MenuSelection(name="Additional label(s)", unit_cost=0.005, qty=0,
                          kind="packaging_per_case"),
            MenuSelection(name="Spare 1 (per case)", unit_cost=0.0, qty=0,
                          kind="packaging_per_case"),
        ],
        packaging_pct=PackagingPcts(waste=0.0, write_off=0.0, storage=0.0),
        box=BoxSelection(code="Бокс 34см", price=0.648),
        equipment=[
            MenuSelection(name="Chep Pallets", unit_cost=10, qty=0, kind="equipment",
                          divide_by_pack=True),
            MenuSelection(name="Add ons", unit_cost=0, qty=0, kind="equipment"),
        ],
        outbound=Outbound(price_per_pallet=50, fill_rate=0.8, boxes_on_order=24,
                          fuel_surcharge_pct=0.0),
        labour=[
            MenuSelection(name="Pack on line", unit_cost=0.10, qty=1, kind="labour"),
            MenuSelection(name="Labelling", unit_cost=0.01, qty=1, kind="labour"),
            MenuSelection(name="Labour 3", unit_cost=0.04, qty=0, kind="labour"),
            MenuSelection(name="Labour 4", unit_cost=0.01, qty=0, kind="labour"),
            MenuSelection(name="Labour 5", unit_cost=0.01, qty=0, kind="labour"),
        ],
        intake_labour_pct=0.10,
        additional_pct=0.10,
        vat_rate=0.20,
        retail_price=5.99,
        selling_price=3.98,
        customer_target_margin=0.35,
        our_target_margin=0.10,
    )


EXPECTED = {
    "materials_total": 2.45,
    "inbound_per_unit": 0.49603174603174605,
    "raw_materials": 2.9754920634920636,
    "packaging_total": 0.0025,
    "box_per_unit": 0.054,
    "pkg_equip_total": 0.0565,
    "outbound_per_unit": 0.21701388888888887,
    "labour_total": 0.121,
    "case_cost": 1.452,
    "operations_total": 0.1331,
    "total_direct_cost": 3.382105952380953,
    "our_gp": 0.5978940476190471,
    "our_gp_pct": 0.1502246350801626,
    "customer_net": 4.991666666666667,
    "customer_gp": 1.0116666666666672,
    "customer_gp_pct": 0.25418760469011736,
    "target_retail": 7.347692307692307,
    "target_selling": 3.757895502645503,
}


def test_golden_workbook_example():
    res = compute(_golden_inputs()).as_floats()
    for key, want in EXPECTED.items():
        assert abs(res[key] - want) < TOL, f"{key}: got {res[key]!r}, want {want!r}"


def test_transport_units_sums_all_material_lines():
    """Deviation (1): transport_units is Σ material qty, not the first line only."""
    inp = _golden_inputs()
    inp.materials = [
        MaterialLine(name="A", unit_cost=1.0, qty=2),
        MaterialLine(name="B", unit_cost=1.0, qty=3),
    ]
    res = compute(inp).as_floats()
    assert abs(res["transport_units"] - 5.0) < TOL
    # inbound scales with the summed transport units: 125/252/1 * 5
    assert abs(res["inbound_per_unit"] - (125 / 252 / 1 * 5)) < TOL


def test_chep_divide_by_pack_with_nonzero_qty():
    """The palletised-equipment formula (cost*qty/boxes_per_cc/qty_per_box) fires
    only when divide_by_pack is set; a plain equipment line is cost*qty."""
    inp = _golden_inputs()
    inp.box = BoxSelection(code=None, price=0.0)          # isolate equipment
    inp.equipment = [
        MenuSelection(name="Chep Pallets", unit_cost=10, qty=2, kind="equipment",
                      divide_by_pack=True),
        MenuSelection(name="Add ons", unit_cost=3, qty=2, kind="equipment"),
    ]
    res = compute(inp).as_floats()
    chep = 10 * 2 / 252 / 1
    addons = 3 * 2
    assert abs(res["equipment_total"] - (chep + addons)) < TOL


def test_zero_upt_does_not_divide_by_zero():
    inp = _golden_inputs()
    inp.units_per_tray = 0
    res = compute(inp).as_floats()          # must not raise
    assert res["box_per_unit"] == 0.0       # price/0 guarded to 0
    assert res["outbound_per_unit"] == 0.0


def test_zero_fill_rate_does_not_divide_by_zero():
    inp = _golden_inputs()
    inp.outbound.fill_rate = 0.0
    res = compute(inp).as_floats()
    assert res["outbound_per_unit"] == 0.0


def test_zero_selling_price_margins_are_none_not_error():
    inp = _golden_inputs()
    inp.selling_price = 0.0
    res = compute(inp).as_floats()
    assert res["our_gp_pct"] is None
    assert res["customer_gp_pct"] is None
    # our_gp is still defined (0 - total_direct_cost)
    assert res["our_gp"] is not None


def test_none_prices_yield_none_margins():
    inp = _golden_inputs()
    inp.selling_price = None
    inp.retail_price = None
    res = compute(inp).as_floats()
    assert res["our_gp"] is None
    assert res["customer_net"] is None
    assert res["customer_gp"] is None
    assert res["target_retail"] is None
    # target_selling depends only on cost + our_target_margin, so it stays defined.
    assert res["target_selling"] is not None


def test_empty_menus_and_materials():
    inp = _golden_inputs()
    inp.materials = []
    inp.packaging = []
    inp.equipment = []
    inp.labour = []
    inp.box = BoxSelection(code=None, price=0.0)
    res = compute(inp).as_floats()
    assert res["materials_total"] == 0.0
    assert res["packaging_total"] == 0.0
    assert res["labour_total"] == 0.0
    # Only outbound remains in the total.
    assert abs(res["total_direct_cost"] - res["outbound_total"]) < TOL
