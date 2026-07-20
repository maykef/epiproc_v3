"""Shared Jinja2 template environment."""
from pathlib import Path

from fastapi.templating import Jinja2Templates

from epiproc.web.csrf import csrf_inject_html

_TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# Single-source the CSRF injector so admin templates share the dashboard's logic.
templates.env.globals["csrf_inject"] = csrf_inject_html
