"""Department-name normalisation.

Single source of truth for collapsing the raw `buyer_department` / `ship_to_*`
/ `sold_to_*` extraction fields into the canonical department names defined
in `configs/departments.yml`. Used by both the Docker dashboard generator
(`scripts/generate_dashboard_v5.py`) and the host FastAPI dashboard
(`dashboard_app/api/db.py`) so the two cannot disagree.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

import yaml

# normalisation.py lives at epiproc/normalisation.py, so parents[1] is the repo
# root and parents[1]/configs is the baked config dir (matches suppliers._REPO_CONFIGS).
# parents[2] was one level too high — it resolved to /configs inside the image
# (WORKDIR /app), which does not exist, so the default silently loaded zero
# department entries. Callers wanting the customer's mounted configs still pass
# configs_dir= explicitly (the dashboard does).
_DEFAULT_CONFIGS_DIR = Path(__file__).resolve().parents[1] / "configs"
_DEPT_ENTRIES_CACHE: dict[Path, list] = {}


def load_dept_entries(configs_dir: Optional[Path] = None) -> list:
    """Load and cache departments.yml entries keyed by configs_dir."""
    cdir = (configs_dir or _DEFAULT_CONFIGS_DIR).resolve()
    cached = _DEPT_ENTRIES_CACHE.get(cdir)
    if cached is not None:
        return cached
    path = cdir / "departments.yml"
    entries = (yaml.safe_load(path.read_text()) or []) if path.exists() else []
    _DEPT_ENTRIES_CACHE[cdir] = entries
    return entries


def dept_from_combo(combo: str, configs_dir: Optional[Path] = None) -> Optional[str]:
    for entry in load_dept_entries(configs_dir):
        if any(p in combo for p in entry["patterns"]):
            return entry["name"]
    # Compound pattern not expressible as simple substrings:
    # addresses that contain "CMR" and "Hills Road/Rd" identify CIMR.
    if "cmr" in combo and ("hills rd" in combo or "hills road" in combo):
        return "CIMR"
    return None


def norm_dept(
    buyer_name: Optional[str],
    buyer_department: Optional[str],
    buyer_address: Optional[str] = None,
    notes: Optional[str] = None,
    your_reference: Optional[str] = None,
    cufs_map: Optional[dict[str, str]] = None,
    order_reference: Optional[str] = None,
    ship_to_name: Optional[str] = None,
    ship_to_department: Optional[str] = None,
    ship_to_address: Optional[str] = None,
    payer_fallback_keywords: Optional[list[str]] = None,
    sold_to_name: Optional[str] = None,
    sold_to_department: Optional[str] = None,
    sold_to_address: Optional[str] = None,
    configs_dir: Optional[Path] = None,
) -> str:
    """Resolve an invoice's free-text address fields to a canonical department."""
    for ref in (your_reference, order_reference):
        if cufs_map and ref:
            m = re.match(r"^([A-Za-z]{2})", ref)
            if m:
                cd = cufs_map.get(m.group(1).upper())
                if cd:
                    return cd
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


def load_cufs_table(db_path: Path) -> dict:
    """Parse `departmental_contacts.xls` sitting next to db_path, return {CUFS_code: dept}."""
    class _TP(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows: list = []
            self._row: list = []
            self._cell = ""
            self._in = False

        def handle_starttag(self, tag, attrs):
            if tag in ("td", "th"):
                self._in = True
                self._cell = ""
            elif tag == "tr":
                self._row = []

        def handle_endtag(self, tag):
            if tag in ("td", "th"):
                self._row.append(self._cell.strip())
                self._in = False
            elif tag == "tr":
                if any(c for c in self._row):
                    self.rows.append(self._row)

        def handle_data(self, d):
            if self._in:
                self._cell += d

    p = _TP()
    try:
        xls = db_path.parent / "departmental_contacts.xls"
        p.feed(xls.read_text(errors="replace"))
    except Exception:
        return {}

    def _label(name: str) -> Optional[str]:
        n = name.upper()
        if "PATHOLOGY" in n and "CIMR" not in n:
            return "Pathology"
        if "SAINSBURY" in n:
            return "Sainsbury Laboratory"
        if "PLANT SCIENCE" in n:
            return "Plant Sciences"
        if "CRUK" in n:
            return "CRUK Cambridge Institute"
        if "STEM CELL" in n:
            return "Stem Cell Institute"
        if "GURDON" in n:
            return "Gurdon Institute"
        if "CIMR" in n:
            return "CIMR"
        if "BIOCHEMISTRY" in n and "CIMR" not in n:
            return "Biochemistry"
        if "GENETICS" in n:
            return "Genetics"
        if "ZOOLOGY" in n:
            return "Zoology"
        return None

    result: dict[str, str] = {}
    for row in p.rows[1:]:
        if len(row) < 2:
            continue
        code = row[1].strip()
        if len(code) == 2 and code.isalpha():
            lbl = _label(row[0].strip())
            if lbl:
                result[code.upper()] = lbl
    return result
