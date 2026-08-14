"""Per-unit product costing engine (replaces the floral_portal Excel workbook).

`calc` is a pure, I/O-free calculation layer (no DB, no network, no LLM); the DB
layer lives in ``epiproc.db.costing`` and the HTTP surface in
``epiproc.web.routers.costing``. Keeping the arithmetic isolated here is what
makes it exhaustively unit-testable against the source workbook.
"""
from __future__ import annotations

from epiproc.costing.calc import (
    BoxSelection,
    CostingInputs,
    CostingResults,
    InboundTransport,
    MaterialLine,
    MenuSelection,
    Outbound,
    PackagingPcts,
    compute,
)

__all__ = [
    "BoxSelection",
    "CostingInputs",
    "CostingResults",
    "InboundTransport",
    "MaterialLine",
    "MenuSelection",
    "Outbound",
    "PackagingPcts",
    "compute",
]
