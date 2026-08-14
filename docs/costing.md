# Per-unit product costing

A per-product costing calculator that replaces the floral_portal Excel workbook.
It computes the total direct cost of packing and distributing one selling unit of
a product, and the resulting gross profit for us and for the customer, plus two
reverse target-price solvers.

**Status:** merged to master and deployed live to the floral_portal instance on
2026-08-14 (`epiproc:3.0.0-50252f9`), Costing tab enabled. The workbook's worked
example was seeded on the instance and verified: all 18 pinned spreadsheet values
(every intermediate and output) match the stored costing to 1e-9.

Like the rest of the engine it is data-free and per-instance: the schema ships in
the shared image (migration `0009_costing.sql`); every product, price, and saved
costing lives in the customer container's own Postgres.

## Layers

| Layer | File | Responsibility |
|---|---|---|
| Migration | `epiproc/db/migrations/0009_costing.sql` | `products`, `box_types`, `cost_menu_items`, `costings` + seeds + `costing_defaults` setting |
| Pure calc | `epiproc/costing/calc.py` | pydantic input/result models + `compute()` — no I/O, no DB, no LLM |
| DB | `epiproc/db/costing.py` | raw-SQL CRUD + versioned `save_costing` + dashboard read model |
| HTTP/UI | `epiproc/web/routers/costing.py` + `templates/admin/costing_*.html` | admin master data + the calculator; customer-facing read-only tab |

The separation is deliberate: all arithmetic is isolated in `calc.py` so it can be
exhaustively unit-tested against the source workbook (`tests/test_costing_calc.py`,
golden case asserted to 1e-9). The DB layer resolves reference prices into the
inputs before calling `compute`, so the calc never touches the database.

## Data model

- **`products`** — master data: `ean` (unique, nullable), `name`, `units_per_tray`
  (UPT), `selling_price` (our price to the client, ex VAT), `retail_price`
  (customer shelf price, inc VAT), `active`.
- **`box_types`** — priced lookup of outbound box models, keyed by `code`.
- **`cost_menu_items`** — reusable packaging / equipment / labour lines, one of four
  `kind`s (`packaging_per_unit`, `packaging_per_case`, `equipment`, `labour`). A line
  is toggled into a costing by giving it a non-zero quantity (the workbook's on/off
  "menu").
- **`costings`** — a saved calculation: a per-product, monotonically-versioned
  (`UNIQUE(product_id, version)`) snapshot of the resolved `inputs` and computed
  `results`, both JSONB, with a `draft`/`final` status.

Per-customer defaults (rates, percentages, target margins) live in the existing
`settings` key/value table under `costing_defaults`; percentages are stored as
fractions (`0.20` = 20%). Money columns are `NUMERIC`; the calc uses `Decimal`.

## Snapshot philosophy

`costings.inputs` embeds the **resolved** prices used at save time — each material
line, each selected menu item's `unit_cost`, and the chosen box price. Editing a
`box_types` or `cost_menu_items` price afterwards therefore never changes a saved
costing. This mirrors `invoices.raw_json`: a stored costing is a faithful,
reproducible record. `results` is the full computed snapshot, so a historical GP is
readable without re-deriving it — and re-running `compute()` on the stored inputs
reproduces the stored results exactly (verified in `tests/test_costing_integration.py`).

Every save recomputes results from the submitted inputs, so a persisted
`inputs`/`results` pair can never drift out of agreement.

## The calculation

`compute()` follows the workbook cell-for-cell; the source cell references appear as
comments in `calc.py` (`# E20`, `# F71`, …). In outline, per selling unit:

```
raw materials      = materials/eur_rate + inbound-per-unit + waste
packaging & equip  = packaging items (+ %s) + box/UPT + equipment
outbound           = price/fill_rate/boxes/UPT + fuel surcharge
operations         = labour (+ intake %) + additional %
total direct cost  = raw materials + packaging & equip + outbound + operations
```

Margins (each `None` when its divisor is zero, never a crash):

```
our GP          = selling_price - total_direct_cost
our GP %        = our GP / selling_price
customer net    = retail_price / (1 + vat_rate)
customer GP     = customer_net - selling_price
customer GP %   = customer GP / selling_price      (on OUR price — workbook fidelity)
target retail   = selling_price / (1 - customer_target_margin) * (1 + vat_rate)
target selling  = total_direct_cost / (1 - our_target_margin)
```

Every division guards its denominator: a zero divisor yields `0` for a cost
component (so totals stay numeric) and `None` for a margin/solver.

## Three deliberate deviations from the workbook

1. **`transport_units` = Σ material line quantities**, not just the first line. The
   workbook's `C23=D18` only referenced line one; summing is correct when a costing
   has more than one material line.
2. **The "Additional Costs" block is a percentage of TOTAL labour** (default 10%),
   and is labelled as such. The workbook mislabelled it.
3. **The sleeve price is an editable menu value** (`Small Sleeve (40cm)` = 0.14 in
   `cost_menu_items`), not a formula literal — so it is maintained as data.

One implementation note: the palletised-equipment formula (`cost*qty ÷ boxes_per_cc
÷ qty_per_box`, used for Chep pallets) is selected by a `divide_by_pack` flag on the
menu selection, kept out of the pure calc as data. The router sets it for the
equipment item named `Chep Pallets`; that name match is customer-specific glue and
lives in `routers/costing.py`, not in the reusable engine.

## UI

- **Admin → Costing** (`/admin/costing/...`, admin-only, CSRF-protected, audited):
  manage products, edit the menus/box-types/defaults, and run the calculator. The
  calculator prefills from the latest saved version (else product + defaults),
  recomputes on **Calculate**, and persists a new version on **Save**.
- **Dashboard → Costing tab** (customer-facing, read-only): each active product with
  its latest *final* costing's total direct cost, selling price, our GP% and
  customer GP%, plus an expandable per-product breakdown. The tab is registered in
  `DASHBOARD_TABS`, so an admin toggles it per customer under Admin → Dashboard; on
  an instance whose visible-tabs were saved before this module shipped it is off
  until explicitly enabled. Data is inlined server-side (no unauthenticated JSON
  route); all product text is escaped on insert and the tab honours the nonce CSP.

The two surfaces are intentionally split: the dashboard tab **publishes** finalised
costings (read-only), and editing lives in the admin calculator (a full-page
recompute on each change). There is currently **no live, user-editable "what-if"
sandbox** — see Future work.

## Future work

- **Interactive costing playground** — a user-editable "what-if" tool where a
  logged-in user changes any input and sees GP/cost recompute live, without a
  full-page reload and without admin rights. Design was scoped but deferred pending
  a client conversation (editable scope: all numbers vs quantities-only; access:
  all users with admin-only save vs sandbox-only). The intended shape keeps a
  **single source of truth**: the browser edits values and calls a server
  `compute()` endpoint (the same `calc.compute`), rather than re-implementing the
  formulas in JavaScript where they could drift from the pinned golden test.
- **Invoice-derived inputs** — the longer-term goal is to pre-populate a product's
  material lines (and eventually other costs) from the instance's own invoice
  history (`invoice_items`, keyed by article/EAN), so a costing starts from real
  paid prices rather than manual entry. The costing math stays deterministic Python
  either way; invoices only seed the inputs. No LLM in the arithmetic path.
