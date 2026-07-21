"""Department-name normalisation.

Collapses the raw `buyer_department` / `ship_to_*` / `sold_to_*` extraction
fields into a canonical department name. It is entirely data-driven: the
patterns come from the customer's own `departments.yml` (resolved per instance),
so the engine hardcodes no organisation, department, or domain. A customer with
no `departments.yml` simply gets every line under "Other".
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

_DEPT_ENTRIES_CACHE: dict[str, list] = {}


def load_dept_entries(configs_dir: Optional[Path] = None) -> list:
    """Load and cache departments.yml, resolved per-file: the customer's mounted
    copy if present, else the repo-baked copy (currently none), else empty. When
    `configs_dir` is given (tests), only that directory is consulted. Cached by the
    resolved path so mounted and baked don't collide."""
    from epiproc.suppliers import resolve_config
    path = resolve_config("departments.yml", configs_dir)
    key = str(path) if path else "__none__"
    cached = _DEPT_ENTRIES_CACHE.get(key)
    if cached is not None:
        return cached
    entries = (yaml.safe_load(path.read_text()) or []) if path else []
    _DEPT_ENTRIES_CACHE[key] = entries
    return entries


def dept_from_combo(combo: str, configs_dir: Optional[Path] = None) -> Optional[str]:
    """Match a lower-cased free-text blob against the customer's department
    patterns. Returns the configured department name, or None if nothing matches."""
    for entry in load_dept_entries(configs_dir):
        if any(p in combo for p in entry["patterns"]):
            return entry["name"]
    return None


def norm_dept(
    buyer_name: Optional[str],
    buyer_department: Optional[str],
    buyer_address: Optional[str] = None,
    notes: Optional[str] = None,
    payer_fallback_keywords: Optional[list[str]] = None,
    ship_to_name: Optional[str] = None,
    ship_to_department: Optional[str] = None,
    ship_to_address: Optional[str] = None,
    sold_to_name: Optional[str] = None,
    sold_to_department: Optional[str] = None,
    sold_to_address: Optional[str] = None,
    configs_dir: Optional[Path] = None,
) -> str:
    """Resolve an invoice's free-text address fields to a canonical department,
    using the customer's configured patterns. Falls back to "Other"."""
    combo = " ".join(filter(None, [buyer_name, buyer_department, buyer_address])).lower()
    sold_to = re.search(r"[Ss]old-to address\s*:\s*(.+?)\.", notes or "")
    if sold_to:
        combo += " " + sold_to.group(1).lower()
    payer_is_passthrough = any(
        kw.lower() in combo for kw in (payer_fallback_keywords or [])
    )
    if not payer_is_passthrough:
        dept = dept_from_combo(combo, configs_dir)
        if dept and dept != "Shared Services":
            return dept
    if ship_to_name or ship_to_department or ship_to_address:
        ship_combo = " ".join(
            filter(None, [ship_to_name, ship_to_department, ship_to_address])
        ).lower()
        dept = dept_from_combo(ship_combo, configs_dir)
        if dept:
            return dept
    if sold_to_name or sold_to_department or sold_to_address:
        sold_combo = " ".join(
            filter(None, [sold_to_name, sold_to_department, sold_to_address])
        ).lower()
        dept = dept_from_combo(sold_combo, configs_dir)
        if dept:
            return dept
    if buyer_department:
        dept = dept_from_combo(buyer_department.lower(), configs_dir)
        if dept and dept != "Shared Services":
            return dept
    if buyer_name:
        dept = dept_from_combo(buyer_name.lower(), configs_dir)
        if dept and dept != "Shared Services":
            return dept
    return "Other"
