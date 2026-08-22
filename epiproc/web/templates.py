"""Shared Jinja2 template environment."""
from pathlib import Path

from fastapi.templating import Jinja2Templates

from epiproc.web.csrf import csrf_inject_html

_TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# Single-source the CSRF injector so admin templates share the dashboard's logic.
templates.env.globals["csrf_inject"] = csrf_inject_html


def _currency_symbol() -> str:
    """This instance's currency symbol (per-customer setting), for money cells.

    A callable global so the symbol resolves per render, not once at import —
    an admin can change it under Admin → Dashboard and the next page shows it.
    """
    from epiproc.db.settings import get_currency_symbol

    return get_currency_symbol()


templates.env.globals["currency"] = _currency_symbol
