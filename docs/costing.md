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
| Migration | `epiproc/db/migrations/0009_costing.sql`, `0010_costing_offer_defaults.sql`, `0011_costing_offer_imports.sql` | `products`, `box_types`, `cost_menu_items`, `costings` + seeds + `costing_defaults` setting (+ engine constants, `offer_imports` batch table) |
| Pure calc | `epiproc/costing/calc.py` | pydantic input/result models + `compute()` — no I/O, no DB, no LLM |
| Offer import | `epiproc/costing/offer_import.py` | offer-sheet parser, row→inputs mapping, batch driver (drafts only) |
| DB | `epiproc/db/costing.py` | raw-SQL CRUD + versioned `save_costing` + `upsert_product_by_key` + dashboard read model |
| HTTP/UI | `epiproc/web/routers/costing.py` + `templates/admin/costing_*.html` | admin master data + the calculator + the offer import wizard; customer-facing read-only tab |

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
  its latest *final* costing's total direct cost and — immediately after it —
  **our price** (total direct cost + 10% — the operator's pricing rule, derived
  at read time from the stored result), then our GP%, customer GP%, and — as the
  final column — the **customer price** (retail inc VAT, the price the customer's
  own shelf shows), plus an expandable per-product breakdown. The tab is registered in
  `DASHBOARD_TABS`, so an admin toggles it per customer under Admin → Dashboard; on
  an instance whose visible-tabs were saved before this module shipped it is off
  until explicitly enabled. Data is inlined server-side (no unauthenticated JSON
  route); all product text is escaped on insert and the tab honours the nonce CSP.
- **Currency** — every money cell across the costing surfaces (products, import
  preview, menus, calculator, dashboard tab) carries the instance's currency
  symbol. The symbol is per-customer data, not engine code: the existing
  `settings.currency_symbol` (Admin → Dashboard) drives it through the Jinja
  `currency()` global; the floral instance is set to `€`.

The two surfaces are intentionally split: the dashboard tab **publishes** finalised
costings (read-only), and editing lives in the admin calculator (a full-page
recompute on each change). There is currently **no live, user-editable "what-if"
sandbox** — see Future work.

## Offer import (batch)

The provider's "offer" workbook (one sheet of hand-typed product lines) can be
imported in batches under **Admin → Costing → Import offer (.xlsx)**:
upload → preview review table → confirm. The preview is a read-only dry run of the
exact same code path, so what the review table shows is what confirm writes —
including intra-batch product merges (two rows sharing one valid EAN become one
created product + one update in the preview too, and the second row shows the
first row's stored price).
_Status: implemented on `feature/costing-offer-import` (2026-08-19); not yet
deployed to live._

**Column mapping** (letters as in the offer file):

| Column | Letter | Use |
|---|---|---|
| Name | B | product name |
| EAN/GTIN | C | product key when a valid 8/13-digit EAN |
| Layers per cc | H | captured on the row, not used by the calc |
| Trays per layer | I | captured on the row, not used by the calc |
| Pcs per tray (UPT) | J | `products.units_per_tray` |
| Per pallet | K | kept as raw text for display only |
| Box height | L | first integer wins (`"48 cm"` → 48, `"20 / 25 cm"` → 20); resolves to the **smallest real box model the height fits into** (34/40/48/80 cm) |
| Material cost | **N** | the material line's unit cost. Column M (CC price) is **deliberately ignored**, per the operator |

Shared values never come from the sheet: all percentages and the inbound/outbound
transport constants come from `costing_defaults` — migration `0010` adds the engine
constants (`pallet_rate` 125, `boxes_per_cc` 252, `qty_per_box` 1, `price_per_pallet`
50, `fill_rate` 0.8, `boxes_on_order` 24, `fuel_surcharge_pct` 0) to both fresh and
already-migrated installs — box prices come from `box_types`, and menu items from
`cost_menu_items` with the default selection Consumables (per case), Pack on line
and Labelling; everything else (including Chep pallets and sleeves) off.

`box_types` holds exactly the four `Бокс` models seeded by migration `0009`
(34/40/48/80 cm). **The operator confirmed (2026-08-19) that no other box
models exist** — the workbook's lookups sheet listed more (Модел 1/2/3), but
those are not real. The offer import therefore maps **every** sheet height into
the smallest of the four models it fits into (`20` → 34, `35` → 40, `60` → 80,
…); only a height taller than 80 cm is unresolvable and flags the row for
attention. Box prices are the real 0009 prices — no placeholders, no imaginary
data.

**Results without manual steps** — the preview and the products list show each
row as the original sheet did: **direct cost → profit → total (selling price)**,
plus the auto retail price and the customer GP%. The selling price is the
product's stored one when it has one (an admin-set price, or a price from an
earlier import, always wins); otherwise the import auto-sets it from the
target-margin formula (`direct cost ÷ (1 − our_target_margin)`, rounded to 2dp —
the workbook's "Target FP", with the seeded 10% margin). The retail price works
the same way: a stored retail wins; otherwise the import auto-sets it from the
workbook's Target Retail formula (`selling ÷ (1 − customer_target_margin) ×
(1 + vat_rate)`, rounded to 2dp — seeded 35% / 20%, the sheet's "7.35 €" cell
for the Roses example), which also fills the customer GP% column (on our price,
workbook fidelity). The chosen prices are written into the saved `inputs`
snapshot, so a costing always records exactly what it computed against.

**Dashboard "Our Price" vs the stored selling** — the customer-facing Costing
tab shows **Our Price** = total direct cost + 10% (the operator's pricing rule,
derived at read time) immediately after the total cost; the stored selling price
is deliberately not shown on that tab. The stored/auto selling (÷ 0.9) and Our
Price (× 1.1) therefore differ by ~1%: the admin surfaces (products list, import
preview) keep the stored/auto selling, and only the dashboard applies the +10%
display rule.

**Each upload is a saved version** — one confirmed upload writes one row in
`offer_imports` (migration `0011`: filename, actor, row/created/updated/costing
counts + the bulk-finalise flag) and archives the uploaded workbook as `data_dir/imports/offer_<id>_<filename>`
(the file is staged at preview time under a server-chosen `.staging_` name and
renamed on confirm; staging files older than 24 h are pruned). Per-product
costings keep their own per-import version bump, so both batch-level and
product-level history exist.

**Invariants**

- **Never auto-finalises** — every imported costing is a `draft` for admin
  review, unless the admin ticks the preview's **bulk-finalise checkbox** (an
  explicit opt-in at confirm: those costings save directly as `final` and the
  batch row records `finalised=true`). Drafts by default, always. Batch approval
  lives **only** at confirm time on the preview screen; there is no post-import
  approval page (a "Batches" list with an Approve button was offered to the
  operator on 2026-08-19, not yet built).
- **Idempotent re-imports** — a row keys to an existing product by valid EAN, else
  exact name; re-importing updates the same product and adds a NEW draft version
  (never duplicate products). An update never clobbers an admin-set selling/retail
  price, nor a stored UPT when the offer row's UPT is missing.
- **One transaction per import** — a mid-batch failure rolls back everything
  (products, costings, the `offer_imports` row).
- Rows that can't be costed (missing UPT / price / box height, or a height with no
  matching `box_types` row) still import as products — without a selling price,
  since the auto-target needs a direct cost — and are flagged **attention** in the
  preview; adding the missing box type under Costing menus and re-importing then
  creates their draft costings.

**Known data-quality caveats** (the file is hand-maintained and the provider says
the format will change between batches): EAN coverage is partial and rows can share
one — a duplicate valid EAN within a batch is flagged rather than silently merged;
junk-length EANs (a `#VALUE!` spill or a partial barcode) are flagged and **never
persisted**, so two products can't collide on the `products.ean` UNIQUE constraint;
column K is informational and varies in format. Sheet text is untrusted input: it is
only ever re-emitted through Jinja auto-escaping (nonce CSP), the confirm step
re-validates the stashed rows server-side, and every confirmed import is
audit-logged (`costing_offer_import` with filename + batch id + archived file +
counts).

Per-product sleeve/packaging refinement (e.g. sleeve by stem length) is deferred:
imports use the default selection above; refine a draft in the calculator.

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
