"""Pure per-unit costing arithmetic — no I/O, fully unit-tested.

This reproduces the floral_portal costing workbook cell-for-cell (the source
cell references appear as comments, e.g. ``# E20``, for traceability). It is
deliberately free of any database, network, or model access: callers assemble a
``CostingInputs`` (the DB layer resolves menu-item/box prices into it first, so a
saved costing is a self-contained snapshot), call :func:`compute`, and get back a
``CostingResults`` snapshot.

Money is handled as ``Decimal`` throughout; the arithmetic never rounds — callers
quantize only at presentation. Percentages are fractions (0.15 == 15%). Every
division guards its denominator: a zero divisor yields ``Decimal(0)`` for a cost
component (so totals stay numeric) and ``None`` for a margin/solver (so an
undefined ratio is reported as "not computable", never a crash).

Three deliberate deviations from the workbook (see docs/costing.md):
  1. ``transport_units`` is the SUM of material line quantities, not just the
     first line — the workbook's ``C23=D18`` only referenced line one.
  2. the "Additional Costs" block is a percentage of TOTAL labour (default 10%),
     correctly labelled here (the workbook mislabelled it).
  3. the sleeve price is an editable menu value (0.14), not a formula literal.
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

# ── Input models ─────────────────────────────────────────────────────────────


class MaterialLine(BaseModel):
    name: str
    unit_cost: float
    qty: float


class InboundTransport(BaseModel):
    qty_per_box: float
    boxes_per_cc: float
    pallet_rate: float


class MenuSelection(BaseModel):
    """A packaging / equipment / labour line pulled from the menu into a costing.

    ``qty == 0`` means the item is toggled off (the workbook's on/off menu). For
    equipment, ``divide_by_pack`` selects the per-CC formula (cost*qty divided by
    boxes-per-CC and qty-per-box) used for palletised items such as Chep pallets;
    the flag is data, so the pure calc stays free of item-name literals.
    """

    name: str
    unit_cost: float
    qty: float
    kind: str
    divide_by_pack: bool = False


class PackagingPcts(BaseModel):
    """Percentages applied to the packaging-items subtotal (each a fraction)."""

    waste: float = 0.0
    write_off: float = 0.0
    storage: float = 0.0


class BoxSelection(BaseModel):
    code: str | None = None
    price: float = 0.0


class Outbound(BaseModel):
    price_per_pallet: float
    fill_rate: float
    boxes_on_order: float
    fuel_surcharge_pct: float = 0.0


class CostingInputs(BaseModel):
    units_per_tray: int                      # UPT
    eur_rate: float                          # material costs are divided by this
    materials: list[MaterialLine] = []
    inbound: InboundTransport
    waste_pct: float = 0.0
    packaging: list[MenuSelection] = []
    packaging_pct: PackagingPcts = PackagingPcts()
    box: BoxSelection = BoxSelection()
    equipment: list[MenuSelection] = []
    outbound: Outbound
    labour: list[MenuSelection] = []
    intake_labour_pct: float = 0.0           # pct of the labour-items subtotal
    additional_pct: float = 0.0              # pct of total labour (incl. intake)
    vat_rate: float = 0.0
    retail_price: float | None = None
    selling_price: float | None = None
    customer_target_margin: float = 0.0
    our_target_margin: float = 0.0


# ── Result model ─────────────────────────────────────────────────────────────


class CostingResults(BaseModel):
    # Raw materials
    materials_total: Decimal
    transport_units: Decimal
    inbound_per_unit: Decimal
    waste: Decimal
    raw_materials: Decimal
    # Packaging & equipment
    pack_items: Decimal
    pack_pcts: Decimal
    packaging_total: Decimal
    box_per_unit: Decimal
    equipment_total: Decimal
    pkg_equip_total: Decimal
    # Outbound distribution
    outbound_per_unit: Decimal
    outbound_at_full: Decimal
    fuel: Decimal
    outbound_total: Decimal
    # Operations / labour
    labour_items: Decimal
    intake: Decimal
    labour_total: Decimal
    case_cost: Decimal
    additional: Decimal
    operations_total: Decimal
    # Grand total
    total_direct_cost: Decimal
    # Margins (None when the ratio is not computable)
    our_gp: Decimal | None = None
    our_gp_pct: Decimal | None = None
    customer_net: Decimal | None = None
    customer_gp: Decimal | None = None
    customer_gp_pct: Decimal | None = None
    target_retail: Decimal | None = None
    target_selling: Decimal | None = None

    def as_floats(self) -> dict:
        """Plain-float dict for JSONB persistence / template rendering (None kept)."""
        return {
            k: (float(v) if isinstance(v, Decimal) else v)
            for k, v in self.model_dump().items()
        }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _D(x) -> Decimal:  # noqa: ANN001
    """Coerce via str so float inputs (2.45) become the exact decimal, not the
    binary-float artefact Decimal(2.45) would give."""
    return Decimal(str(x))


def _div(a: Decimal, b: Decimal) -> Decimal:
    """Divide, but treat a zero denominator as 0 rather than raising — keeps a
    cost component numeric when a rate/count is left at zero."""
    return a / b if b != 0 else Decimal(0)


def _ratio(a: Decimal, b: Decimal) -> Decimal | None:
    """Ratio for margins/solvers: a zero denominator means 'not computable'."""
    return a / b if b != 0 else None


# ── The computation ──────────────────────────────────────────────────────────


def compute(inp: CostingInputs) -> CostingResults:
    upt = _D(inp.units_per_tray)
    eur_rate = _D(inp.eur_rate)

    # ── Raw materials ────────────────────────────────────────────────────────
    materials_sum = sum((_D(m.unit_cost) * _D(m.qty) for m in inp.materials), Decimal(0))
    materials_total = _div(materials_sum, eur_rate)                          # E20

    transport_units = sum((_D(m.qty) for m in inp.materials), Decimal(0))    # C23 (Σ, not line 1)
    per_box = _div(_div(_D(inp.inbound.pallet_rate), _D(inp.inbound.boxes_per_cc)),
                   _D(inp.inbound.qty_per_box))
    inbound_per_unit = per_box * transport_units                            # E26

    waste = (materials_total + inbound_per_unit) * _D(inp.waste_pct)         # E31
    raw_materials = materials_total + inbound_per_unit + waste               # F33

    # ── Packaging ────────────────────────────────────────────────────────────
    pack_items = Decimal(0)                                                  # E36..E41
    for p in inp.packaging:
        line = _D(p.unit_cost) * _D(p.qty)
        if p.kind == "packaging_per_case":
            line = _div(line, upt)
        pack_items += line
    pct = inp.packaging_pct
    pack_pcts = pack_items * (_D(pct.waste) + _D(pct.write_off) + _D(pct.storage))  # E42..E44
    packaging_total = pack_items + pack_pcts                                 # E45

    box_per_unit = _div(_D(inp.box.price), upt)                             # E47
    equipment_total = box_per_unit
    for e in inp.equipment:                                                  # E48..E49
        if e.divide_by_pack:
            equipment_total += _div(_div(_D(e.unit_cost) * _D(e.qty),
                                         _D(inp.inbound.boxes_per_cc)),
                                    _D(inp.inbound.qty_per_box))
        else:
            equipment_total += _D(e.unit_cost) * _D(e.qty)
    pkg_equip_total = packaging_total + equipment_total                     # F51

    # ── Outbound distribution ────────────────────────────────────────────────
    ob = inp.outbound
    outbound_per_unit = _div(_div(_div(_D(ob.price_per_pallet), _D(ob.fill_rate)),
                                  _D(ob.boxes_on_order)), upt)              # E54
    outbound_at_full = _div(_div(_D(ob.price_per_pallet), _D(ob.boxes_on_order)),
                            upt)                                            # F54 (fill_rate=1, info)
    fuel = outbound_per_unit * _D(ob.fuel_surcharge_pct)                    # E55
    outbound_total = outbound_per_unit + fuel                              # E56

    # ── Operations / labour ──────────────────────────────────────────────────
    labour_items = sum((_D(x.unit_cost) * _D(x.qty) for x in inp.labour), Decimal(0))  # E60..E64
    intake = labour_items * _D(inp.intake_labour_pct)                      # E65
    labour_total = labour_items + intake                                   # E66
    case_cost = labour_total * upt                                         # E67
    additional = labour_total * _D(inp.additional_pct)                     # E68
    operations_total = labour_total + additional                          # F69

    # ── Grand total ──────────────────────────────────────────────────────────
    total_direct_cost = (raw_materials + pkg_equip_total
                         + outbound_total + operations_total)             # F71

    # ── Margins & reverse solvers ────────────────────────────────────────────
    selling = _D(inp.selling_price) if inp.selling_price is not None else None
    retail = _D(inp.retail_price) if inp.retail_price is not None else None
    vat = _D(inp.vat_rate)

    our_gp = (selling - total_direct_cost) if selling is not None else None            # E11
    our_gp_pct = _ratio(our_gp, selling) if (our_gp is not None and selling is not None) else None  # E12

    customer_net = _ratio(retail, Decimal(1) + vat) if retail is not None else None    # E6
    customer_gp = (customer_net - selling) if (customer_net is not None and selling is not None) else None  # E7
    # E8 — deliberately divides by OUR selling price, not customer_net (workbook fidelity).
    customer_gp_pct = _ratio(customer_gp, selling) if (customer_gp is not None and selling is not None) else None

    target_retail = None                                                                # F6
    if selling is not None:
        base = _ratio(selling, Decimal(1) - _D(inp.customer_target_margin))
        if base is not None:
            target_retail = base * (Decimal(1) + vat)
    target_selling = _ratio(total_direct_cost, Decimal(1) - _D(inp.our_target_margin))  # F12

    return CostingResults(
        materials_total=materials_total,
        transport_units=transport_units,
        inbound_per_unit=inbound_per_unit,
        waste=waste,
        raw_materials=raw_materials,
        pack_items=pack_items,
        pack_pcts=pack_pcts,
        packaging_total=packaging_total,
        box_per_unit=box_per_unit,
        equipment_total=equipment_total,
        pkg_equip_total=pkg_equip_total,
        outbound_per_unit=outbound_per_unit,
        outbound_at_full=outbound_at_full,
        fuel=fuel,
        outbound_total=outbound_total,
        labour_items=labour_items,
        intake=intake,
        labour_total=labour_total,
        case_cost=case_cost,
        additional=additional,
        operations_total=operations_total,
        total_direct_cost=total_direct_cost,
        our_gp=our_gp,
        our_gp_pct=our_gp_pct,
        customer_net=customer_net,
        customer_gp=customer_gp,
        customer_gp_pct=customer_gp_pct,
        target_retail=target_retail,
        target_selling=target_selling,
    )
