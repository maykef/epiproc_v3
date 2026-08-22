"""Per-unit product costing — admin master data + the calculator.

Mirrors the admin-router idioms: GET behind ``get_session_user``, every mutation
gated by the ``_admin`` role guard, CSRF handled by the global middleware, and
each mutation written to the audit log. All arithmetic is delegated to the pure
``epiproc.costing.calc`` layer; this router only marshals form data in and renders
results out. No LLM calls anywhere.

Snapshot flow: the calculator resolves each selected menu item's unit cost and
the chosen box price from the DB into a ``CostingInputs``, computes, and (on save)
persists BOTH the resolved inputs and the computed results — so a saved costing is
reproducible and immune to later edits of the reference tables.
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from urllib.parse import quote
from zipfile import BadZipFile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from openpyxl.utils.exceptions import InvalidFileException

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
from epiproc.costing.offer_import import OfferRow, parse_offer, run_offer_import
from epiproc.db import costing as db
from epiproc.settings import settings
from epiproc.web.security import _request_ip, audit_log
from epiproc.web.session import get_session_user
from epiproc.web.templates import templates

router = APIRouter(include_in_schema=False)

# The workbook's palletised-equipment line uses a per-CC divisor; keyed by name
# here (customer-specific glue) so the pure calc stays free of item-name literals.
_PER_PACK_EQUIPMENT = {"chep pallets"}


def _admin(request: Request) -> dict:
    user = get_session_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


def _audit(request: Request, me: dict, action: str, detail: dict | None = None) -> None:
    audit_log(
        action=action,
        username=me["username"],
        ip=_request_ip(request),
        resource=request.url.path,
        detail=detail,
        user_agent=request.headers.get("user-agent", ""),
    )


def _enc(s: str) -> str:
    return quote(s, safe="")


def _f(form, key: str, default: float = 0.0) -> float:  # noqa: ANN001
    v = form.get(key)
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _f_or_none(form, key: str) -> float | None:  # noqa: ANN001
    v = form.get(key)
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ── Products ─────────────────────────────────────────────────────────────────


@router.get("/admin/costing")
def costing_home(request: Request):
    _admin(request)
    return RedirectResponse("/admin/costing/products", status_code=307)


@router.get("/admin/costing/products")
def products_page(request: Request, ok: str = "", err: str = ""):
    me = _admin(request)
    products = db.list_products()
    # Annotate with the latest costing regardless of status: drafts are the
    # freshly imported results and must show here immediately (the status badge
    # says whether an admin has finalised them yet).
    for p in products:
        latest = db.get_latest_costing(p["id"])
        p["latest_version"] = latest["version"] if latest else None
        p["latest_status"] = latest["status"] if latest else None
        p["latest_cost"] = (latest["results"] or {}).get("total_direct_cost") if latest else None
        p["latest_gp"] = (latest["results"] or {}).get("our_gp") if latest else None
        p["latest_customer_gp_pct"] = (
            (latest["results"] or {}).get("customer_gp_pct") if latest else None)
    return templates.TemplateResponse(request, "admin/costing_products.html", {
        "me": me["username"],
        "products": products,
        "flash_msg": ok or err,
        "flash_kind": "ok" if ok else ("err" if err else ""),
    })


@router.post("/admin/costing/products")
async def products_save(request: Request):
    me = _admin(request)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return RedirectResponse(f"/admin/costing/products?err={_enc('Product name is required.')}",
                                status_code=303)
    pid_raw = (form.get("product_id") or "").strip()
    try:
        product_id = db.upsert_product(
            name=name,
            ean=(form.get("ean") or "").strip() or None,
            units_per_tray=int(_f(form, "units_per_tray", 1)),
            retail_price=_f_or_none(form, "retail_price"),
            selling_price=_f_or_none(form, "selling_price"),
            active=(form.get("active") is not None),
            product_id=int(pid_raw) if pid_raw else None,
        )
    except Exception as exc:  # noqa: BLE001 — e.g. duplicate EAN
        return RedirectResponse(f"/admin/costing/products?err={_enc(str(exc))}", status_code=303)
    _audit(request, me, "costing_product_save", {"product_id": product_id, "name": name})
    return RedirectResponse(f"/admin/costing/products?ok={_enc('Product saved.')}", status_code=303)


# ── Menus / box types / defaults ─────────────────────────────────────────────


@router.get("/admin/costing/menus")
def menus_page(request: Request, ok: str = "", err: str = ""):
    me = _admin(request)
    grouped = {k: db.list_menu_items(kind=k) for k in db.MENU_KINDS}
    return templates.TemplateResponse(request, "admin/costing_menus.html", {
        "me": me["username"],
        "menu_kinds": db.MENU_KINDS,
        "grouped": grouped,
        "box_types": db.list_box_types(),
        "defaults": db.get_costing_defaults(),
        "flash_msg": ok or err,
        "flash_kind": "ok" if ok else ("err" if err else ""),
    })


@router.post("/admin/costing/menus/item")
async def menus_save_item(request: Request):
    me = _admin(request)
    form = await request.form()
    kind = (form.get("kind") or "").strip()
    name = (form.get("name") or "").strip()
    if not name or kind not in db.MENU_KINDS:
        return RedirectResponse(f"/admin/costing/menus?err={_enc('Name and a valid kind are required.')}",
                                status_code=303)
    iid = (form.get("item_id") or "").strip()
    try:
        db.upsert_menu_item(
            kind=kind, name=name, unit_cost=_f(form, "unit_cost", 0.0),
            active=(form.get("active") is not None),
            sort_order=int(_f(form, "sort_order", 0)),
            item_id=int(iid) if iid else None,
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"/admin/costing/menus?err={_enc(str(exc))}", status_code=303)
    _audit(request, me, "costing_menu_item_save", {"kind": kind, "name": name})
    return RedirectResponse(f"/admin/costing/menus?ok={_enc('Menu item saved.')}", status_code=303)


@router.post("/admin/costing/menus/box")
async def menus_save_box(request: Request):
    me = _admin(request)
    form = await request.form()
    code = (form.get("code") or "").strip()
    if not code:
        return RedirectResponse(f"/admin/costing/menus?err={_enc('Box code is required.')}",
                                status_code=303)
    bid = (form.get("box_id") or "").strip()
    try:
        db.upsert_box_type(
            code=code, price=_f(form, "price", 0.0),
            model=(form.get("model") or "").strip() or None,
            dimensions=(form.get("dimensions") or "").strip() or None,
            active=(form.get("active") is not None),
            box_id=int(bid) if bid else None,
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"/admin/costing/menus?err={_enc(str(exc))}", status_code=303)
    _audit(request, me, "costing_box_save", {"code": code})
    return RedirectResponse(f"/admin/costing/menus?ok={_enc('Box type saved.')}", status_code=303)


@router.post("/admin/costing/defaults")
async def defaults_save(request: Request):
    me = _admin(request)
    form = await request.form()
    keys = db.DEFAULT_COSTING_DEFAULTS.keys()
    values = {k: _f(form, k, db.DEFAULT_COSTING_DEFAULTS[k]) for k in keys}
    db.set_costing_defaults(values)
    _audit(request, me, "costing_defaults_save", values)
    return RedirectResponse(f"/admin/costing/menus?ok={_enc('Defaults saved.')}", status_code=303)


# ── The calculator ───────────────────────────────────────────────────────────


def _menu_view(items: list[dict], saved_qty: dict) -> list[dict]:
    """Menu rows for the form: active items with a prefilled qty (matched by name
    from a saved costing, else 0)."""
    out = []
    for it in items:
        out.append({
            "id": it["id"],
            "name": it["name"],
            "unit_cost": it["unit_cost"],
            "qty": saved_qty.get(it["name"], 0.0),
        })
    return out


def _calc_context(request: Request, me: dict, product: dict, *,
                  saved_inputs: dict | None, results: dict | None,
                  flash_msg: str = "", flash_kind: str = "") -> dict:
    defaults = db.get_costing_defaults()
    box_types = db.list_box_types(active_only=True)
    si = saved_inputs or {}

    # Scalars: prefer a saved costing's value, else the product/defaults.
    def sc(key, fallback):
        v = si.get(key)
        return v if v is not None else fallback

    inbound = si.get("inbound") or {}
    outbound = si.get("outbound") or {}
    pack_pct = si.get("packaging_pct") or {}
    box = si.get("box") or {}

    # packaging is one saved list mixing per_unit/per_case; qty matched by name.
    pack_saved = si.get("packaging") or []
    pack_qty = {s["name"]: s["qty"] for s in pack_saved if isinstance(s, dict)}
    equip_qty = {s["name"]: s["qty"] for s in (si.get("equipment") or []) if isinstance(s, dict)}
    labour_qty = {s["name"]: s["qty"] for s in (si.get("labour") or []) if isinstance(s, dict)}

    materials = si.get("materials") or []
    # Always offer a few blank rows for adding materials.
    material_rows = [dict(m) for m in materials] + [{"name": "", "unit_cost": "", "qty": ""}
                                                    for _ in range(3)]

    return {
        "me": me["username"],
        "product": product,
        "defaults": defaults,
        "box_types": box_types,
        "results": results,
        "material_rows": material_rows,
        "pack_unit_items": _menu_view(db.list_menu_items("packaging_per_unit", active_only=True), pack_qty),
        "pack_case_items": _menu_view(db.list_menu_items("packaging_per_case", active_only=True), pack_qty),
        "equipment_items": _menu_view(db.list_menu_items("equipment", active_only=True), equip_qty),
        "labour_items": _menu_view(db.list_menu_items("labour", active_only=True), labour_qty),
        "f": {
            "units_per_tray": sc("units_per_tray", product.get("units_per_tray") or 1),
            "eur_rate": sc("eur_rate", defaults["eur_rate"]),
            "vat_rate": sc("vat_rate", defaults["vat_rate"]),
            "waste_pct": sc("waste_pct", defaults["waste_pct"]),
            "intake_labour_pct": sc("intake_labour_pct", defaults["intake_labour_pct"]),
            "additional_pct": sc("additional_pct", defaults["additional_pct"]),
            "customer_target_margin": sc("customer_target_margin", defaults["customer_target_margin"]),
            "our_target_margin": sc("our_target_margin", defaults["our_target_margin"]),
            "inbound_qty_per_box": inbound.get("qty_per_box", 1),
            "inbound_boxes_per_cc": inbound.get("boxes_per_cc", 0),
            "inbound_pallet_rate": inbound.get("pallet_rate", 0),
            "pack_waste": pack_pct.get("waste", 0),
            "pack_write_off": pack_pct.get("write_off", 0),
            "pack_storage": pack_pct.get("storage", 0),
            "ob_price_per_pallet": outbound.get("price_per_pallet", 0),
            "ob_fill_rate": outbound.get("fill_rate", 1),
            "ob_boxes_on_order": outbound.get("boxes_on_order", 0),
            "ob_fuel_surcharge_pct": outbound.get("fuel_surcharge_pct", 0),
            "box_code": box.get("code"),
            "retail_price": sc("retail_price", product.get("retail_price")),
            "selling_price": sc("selling_price", product.get("selling_price")),
        },
        "flash_msg": flash_msg,
        "flash_kind": flash_kind,
    }


def _parse_inputs(form, product: dict) -> CostingInputs:  # noqa: ANN001
    """Build a fully-resolved CostingInputs from the calculator form, snapshotting
    each menu item's unit cost and the chosen box price from the DB."""
    upt = int(_f(form, "units_per_tray", product.get("units_per_tray") or 1))

    # Materials (parallel arrays; blank-name rows dropped).
    names = form.getlist("material_name")
    costs = form.getlist("material_cost")
    qtys = form.getlist("material_qty")
    materials: list[MaterialLine] = []
    for i, nm in enumerate(names):
        nm = (nm or "").strip()
        if not nm:
            continue
        c = costs[i] if i < len(costs) else "0"
        q = qtys[i] if i < len(qtys) else "0"
        materials.append(MaterialLine(
            name=nm,
            unit_cost=float(c) if str(c).strip() else 0.0,
            qty=float(q) if str(q).strip() else 0.0,
        ))

    def selections(kind: str) -> list[MenuSelection]:
        out = []
        for it in db.list_menu_items(kind=kind, active_only=True):
            qty = _f(form, f"qty_{it['id']}", 0.0)
            out.append(MenuSelection(
                name=it["name"], unit_cost=float(it["unit_cost"]), qty=qty, kind=kind,
                divide_by_pack=(kind == "equipment"
                                and it["name"].strip().lower() in _PER_PACK_EQUIPMENT),
            ))
        return out

    packaging = selections("packaging_per_unit") + selections("packaging_per_case")

    # Box: resolve the chosen id -> code + price snapshot.
    box = BoxSelection()
    bid = (form.get("box_id") or "").strip()
    if bid:
        bt = db.get_box_type(int(bid))
        if bt:
            box = BoxSelection(code=bt["code"], price=float(bt["price"]))

    return CostingInputs(
        units_per_tray=upt,
        eur_rate=_f(form, "eur_rate", 1.0),
        materials=materials,
        inbound=InboundTransport(
            qty_per_box=_f(form, "inbound_qty_per_box", 1.0),
            boxes_per_cc=_f(form, "inbound_boxes_per_cc", 0.0),
            pallet_rate=_f(form, "inbound_pallet_rate", 0.0),
        ),
        waste_pct=_f(form, "waste_pct", 0.0),
        packaging=packaging,
        packaging_pct=PackagingPcts(
            waste=_f(form, "pack_waste", 0.0),
            write_off=_f(form, "pack_write_off", 0.0),
            storage=_f(form, "pack_storage", 0.0),
        ),
        box=box,
        equipment=selections("equipment"),
        outbound=Outbound(
            price_per_pallet=_f(form, "ob_price_per_pallet", 0.0),
            fill_rate=_f(form, "ob_fill_rate", 1.0),
            boxes_on_order=_f(form, "ob_boxes_on_order", 0.0),
            fuel_surcharge_pct=_f(form, "ob_fuel_surcharge_pct", 0.0),
        ),
        labour=selections("labour"),
        intake_labour_pct=_f(form, "intake_labour_pct", 0.0),
        additional_pct=_f(form, "additional_pct", 0.0),
        vat_rate=_f(form, "vat_rate", 0.0),
        retail_price=_f_or_none(form, "retail_price"),
        selling_price=_f_or_none(form, "selling_price"),
        customer_target_margin=_f(form, "customer_target_margin", 0.0),
        our_target_margin=_f(form, "our_target_margin", 0.0),
    )


@router.get("/admin/costing/products/{product_id}/cost")
def calculator_page(request: Request, product_id: int):
    me = _admin(request)
    product = db.get_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")
    latest = db.get_latest_costing(product_id)
    saved_inputs = latest["inputs"] if latest else None
    results = latest["results"] if latest else None
    ctx = _calc_context(request, me, product, saved_inputs=saved_inputs, results=results)
    if latest:
        ctx["flash_msg"] = f"Prefilled from version {latest['version']} ({latest['status']})."
        ctx["flash_kind"] = "ok"
    return templates.TemplateResponse(request, "admin/costing_calculator.html", ctx)


@router.post("/admin/costing/products/{product_id}/cost/calculate")
async def calculator_calculate(request: Request, product_id: int):
    me = _admin(request)
    product = db.get_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")
    form = await request.form()
    inp = _parse_inputs(form, product)
    results = compute(inp).as_floats()
    ctx = _calc_context(request, me, product, saved_inputs=inp.model_dump(), results=results)
    ctx["flash_msg"] = "Calculated (not yet saved)."
    ctx["flash_kind"] = "ok"
    return templates.TemplateResponse(request, "admin/costing_calculator.html", ctx)


@router.post("/admin/costing/products/{product_id}/cost/save")
async def calculator_save(request: Request, product_id: int):
    me = _admin(request)
    product = db.get_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")
    form = await request.form()
    inp = _parse_inputs(form, product)
    results = compute(inp).as_floats()                 # recompute so inputs↔results always agree
    status = "final" if (form.get("status") == "final") else "draft"
    saved = db.save_costing(product_id, inp.model_dump(), results,
                            created_by=me["username"], status=status)
    _audit(request, me, "costing_save", {"product_id": product_id, "version": saved["version"],
                                         "status": status})
    ctx = _calc_context(request, me, product, saved_inputs=inp.model_dump(), results=results)
    ctx["flash_msg"] = f"Saved version {saved['version']} ({status})."
    ctx["flash_kind"] = "ok"
    return templates.TemplateResponse(request, "admin/costing_calculator.html", ctx)


# ── Offer-sheet batch import ─────────────────────────────────────────────────
#
# Three steps, mirroring the phase-1 flow: GET /import shows the upload form;
# POST /import/preview parses + dry-runs (writes nothing) and renders the review
# table; POST /import/confirm re-validates the stashed rows server-side and runs
# the real import in ONE transaction. The parsed rows travel as a JSON payload
# in a hidden textarea — sheet text is untrusted input, so it is only ever
# re-emitted through Jinja's auto-escape, and the confirm step never trusts the
# payload blindly (every dict is re-validated into an OfferRow).

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # offer workbooks are ~9 MB (embedded photos)

_IMPORTS_DIR = Path(settings.data_dir) / "imports"


def _stage_upload(data: bytes, filename: str) -> str:
    """Stash the uploaded workbook on disk under a server-chosen name so the
    confirm step can archive it as the batch's file (one upload = one saved
    version). Returns the staging name ("" when there is nothing to stash)."""
    if not data:
        return ""
    _IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    name = f".staging_{secrets.token_hex(8)}_{Path(filename).name}"
    (_IMPORTS_DIR / name).write_bytes(data)
    # Opportunistically drop stale staging files (uploads never confirmed).
    cutoff = time.time() - 86400
    for p in _IMPORTS_DIR.glob(".staging_*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass
    return name


@router.get("/admin/costing/import")
def import_page(request: Request, ok: str = "", err: str = ""):
    me = _admin(request)
    return templates.TemplateResponse(request, "admin/costing_import.html", {
        "me": me["username"],
        "flash_msg": ok or err,
        "flash_kind": "ok" if ok else ("err" if err else ""),
    })


def _import_form_error(request: Request, me: dict, msg: str):
    return templates.TemplateResponse(request, "admin/costing_import.html", {
        "me": me["username"], "flash_msg": msg, "flash_kind": "err",
    }, status_code=422)


@router.post("/admin/costing/import/preview")
async def import_preview(request: Request):
    me = _admin(request)
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return _import_form_error(request, me, "No file selected.")
    filename = upload.filename
    if not filename.lower().endswith(".xlsx"):
        return _import_form_error(request, me, "Only .xlsx offer workbooks are accepted.")
    size = getattr(upload, "size", None)
    if size and size > _MAX_UPLOAD_BYTES:
        return _import_form_error(request, me, "Upload too large (max 10 MB).")
    data = await upload.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        return _import_form_error(request, me, "Upload too large (max 10 MB).")
    try:
        rows = parse_offer(data)
    except (BadZipFile, InvalidFileException, KeyError, IndexError, OSError, ValueError) as exc:
        return _import_form_error(request, me, f"Could not read that workbook: {exc}")
    if not any(r.name for r in rows):
        return _import_form_error(request, me, "No product rows detected in that workbook.")
    report = run_offer_import(rows, actor=me["username"], dry_run=True, source=filename)
    attention = sum(1 for r in report.rows if r.status == "attention")
    return templates.TemplateResponse(request, "admin/costing_import_preview.html", {
        "me": me["username"],
        "filename": filename,
        "report": report,
        "attention": attention,
        "staging_name": _stage_upload(data, filename),
        "payload_json": json.dumps([r.model_dump(mode="json") for r in rows]),
    })


@router.post("/admin/costing/import/confirm")
async def import_confirm(request: Request):
    me = _admin(request)
    form = await request.form()
    filename = (form.get("filename") or "offer.xlsx").strip() or "offer.xlsx"
    payload_raw = form.get("payload") or ""
    try:
        data = json.loads(payload_raw)
        if not isinstance(data, list):
            raise ValueError("payload is not a list")
        rows = [OfferRow(**d) for d in data]
    except (ValueError, TypeError):
        return RedirectResponse(
            "/admin/costing/import?err="
            + _enc("Invalid import payload — please re-upload the workbook."),
            status_code=303)
    if not any(r.name for r in rows):
        return RedirectResponse(
            "/admin/costing/import?err="
            + _enc("No product rows in that payload — please re-upload."),
            status_code=303)
    staging_name = (form.get("staging") or "").strip()
    staging_path = None
    if (staging_name.startswith(".staging_")
            and Path(staging_name).name == staging_name):  # no path traversal
        staging_path = _IMPORTS_DIR / staging_name
    finalised = form.get("finalise") in ("1", "on", "true")
    try:
        report = run_offer_import(
            rows, actor=me["username"], dry_run=False, source=filename,
            finalise=finalised)
    except Exception as exc:  # noqa: BLE001 — the run's transaction already rolled back
        if staging_path is not None:
            staging_path.unlink(missing_ok=True)
        return RedirectResponse(
            "/admin/costing/import?err=" + _enc(f"Import failed, nothing was saved: {exc}"),
            status_code=303)
    archived = ""
    if staging_path is not None and staging_path.is_file() and report.batch_id:
        archived = f"offer_{report.batch_id}_{Path(filename).name}"
        staging_path.replace(_IMPORTS_DIR / archived)   # one upload = one saved file
    elif staging_path is not None:
        staging_path.unlink(missing_ok=True)
    _audit(request, me, "costing_offer_import", {
        "filename": filename,
        "batch_id": report.batch_id,
        "archived_file": archived,
        "products_created": report.products_created,
        "products_updated": report.products_updated,
        "costings_created": report.costings_created,
        "skipped": report.skipped,
        "finalised": finalised,
    })
    attention = sum(1 for r in report.rows if r.status == "attention")
    total_products = report.products_created + report.products_updated
    status_word = "final" if finalised else "draft"
    msg = (f"Import complete: {total_products} products, "
           f"{report.costings_created} {status_word} costings, {attention} need attention. "
           f"Saved as batch #{report.batch_id}.")
    return RedirectResponse(f"/admin/costing/products?ok={_enc(msg)}", status_code=303)
