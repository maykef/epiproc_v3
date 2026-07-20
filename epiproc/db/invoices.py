"""Write an extracted+corrected record into invoices / invoice_items.

Maps the nested extraction dict onto the flat v3 schema (migration 0001).
Idempotent on (supplier, filename): re-processing replaces the prior rows.
"""
from __future__ import annotations

import datetime
import json


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _date(v):
    """Normalise an extracted invoice_date to a date, or None. invoice_date is a
    DATE column now, so a malformed string must become NULL rather than raise and
    abort the insert."""
    if isinstance(v, datetime.date):
        return v
    if isinstance(v, str) and len(v) >= 10:
        try:
            return datetime.date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def insert_record(conn, supplier: str, filename: str, record: dict,
                  corrections: list[str], error: str | None = None,
                  status: str = "extracted", processing_time_s: float | None = None) -> int:
    rec = record or {}
    tot = rec.get("totals") or {}
    seller = rec.get("seller") or {}
    buyer = rec.get("buyer") or {}
    ship = rec.get("ship_to") or {}
    sold = rec.get("sold_to") or {}
    refs = rec.get("references") or {}

    conn.execute("DELETE FROM invoices WHERE supplier=%s AND filename=%s", (supplier, filename))
    row = conn.execute(
        """INSERT INTO invoices
           (supplier, filename, document_type, invoice_number, invoice_date, currency,
            seller_name, buyer_name, buyer_department, buyer_address, buyer_customer_number,
            ship_to_name, ship_to_department, ship_to_address,
            sold_to_name, sold_to_department, sold_to_address,
            your_reference, order_reference, payment_terms,
            subtotal, discount_rate_percent, discount_amount, discount_2,
            freight, handling_charges, vat_amount, total_amount,
            notes, corrections_applied, validation_warning,
            raw_json, extraction_error, processing_time_s, status)
           VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s,
                   %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s)
           RETURNING id""",
        (supplier, filename, rec.get("document_type"), rec.get("invoice_number"),
         _date(rec.get("invoice_date")), rec.get("currency"),
         seller.get("name"), buyer.get("name"), buyer.get("department"),
         buyer.get("address"), buyer.get("customer_number"),
         ship.get("name"), ship.get("department"), ship.get("address"),
         sold.get("name"), sold.get("department"), sold.get("address"),
         refs.get("your_reference"), refs.get("order_reference"), rec.get("payment_terms"),
         _num(tot.get("subtotal")), _num(tot.get("discount_rate_percent")),
         _num(tot.get("discount_amount")), _num(tot.get("discount_2")),
         _num(tot.get("freight")), _num(tot.get("handling_charges")),
         _num(tot.get("vat_amount")), _num(tot.get("total")),
         rec.get("notes"), "; ".join(corrections) if corrections else None,
         rec.get("validation_warning"),
         json.dumps(rec, ensure_ascii=False), error, processing_time_s, status),
    ).fetchone()
    inv_id = row["id"]

    for it in rec.get("line_items") or []:
        if not isinstance(it, dict):
            continue
        conn.execute(
            """INSERT INTO invoice_items
               (invoice_id, position, article, quantity, unit, description,
                unit_price, total_price, line_discount_amount)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (inv_id, it.get("position"), it.get("article"), _num(it.get("quantity")),
             it.get("unit"), it.get("description"), _num(it.get("unit_price")),
             _num(it.get("total_price")), _num(it.get("line_discount_amount"))),
        )
    conn.commit()
    return inv_id
