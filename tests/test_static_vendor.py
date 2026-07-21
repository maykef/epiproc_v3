"""Front-end libs are vendored + served same-origin; the CSP trusts no CDN.

Regression for the external-review point: chart.js/d3/d3-sankey used to load from
cdn.jsdelivr.net with the whole origin whitelisted in script-src, which undercut the
nonce CSP. They are now committed under web/static/vendor, served at /static, and the
CSP script-src/font-src no longer name any CDN.
"""
from __future__ import annotations

import base64
import hashlib
import pathlib

import pytest

_VENDOR = pathlib.Path(__file__).resolve().parent.parent / "epiproc" / "web" / "static" / "vendor"
_TEMPLATE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "epiproc" / "web" / "templates" / "dashboard_template.html"
)

# filename -> the SRI hash embedded in the template (kept in sync deliberately).
_EXPECTED = {
    "chart.umd.min.js": "sha384-NrKB+u6Ts6AtkIhwPixiKTzgSKNblyhlk0Sohlgar9UHUBzai/sgnNNWWd291xqt",
    "d3.min.js": "sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i",
    "d3-sankey.min.js": "sha384-SM54CE5h+qdDI046d2Y5ym7wq1kq4uxcQ1cqGq5/+5jrE5tPLeDJSq711Q8sIska",
}


def _sri(data: bytes) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()


@pytest.mark.parametrize("name,expected", _EXPECTED.items())
def test_vendored_file_matches_its_sri(name, expected):
    f = _VENDOR / name
    assert f.exists(), f"missing vendored lib {name}"
    data = f.read_bytes()
    assert data, f"{name} is empty"
    assert _sri(data) == expected, (
        f"{name} content no longer matches the SRI hash in the template — "
        f"update the integrity attr if the bump is intentional"
    )


def test_template_references_vendored_libs_not_cdn():
    html = _TEMPLATE.read_text()
    assert "jsdelivr" not in html and "cdn." not in html, "template still references a CDN"
    for name, expected in _EXPECTED.items():
        assert f"/static/vendor/{name}" in html
        assert expected in html, f"SRI for {name} not present in template"


def test_csp_names_no_cdn_origin():
    pytest.importorskip("fastapi")
    from epiproc.web.security import _csp

    csp = _csp("NONCE")
    assert "jsdelivr" not in csp and "cdn." not in csp
    assert "script-src 'self' 'nonce-NONCE'" in csp
    assert "font-src 'self';" in csp


def test_static_mount_serves_vendor_dir():
    pytest.importorskip("fastapi")
    from starlette.applications import Starlette
    from starlette.staticfiles import StaticFiles
    from starlette.testclient import TestClient

    # Mount the real vendor dir standalone (avoids booting the app's DB lifespan).
    app = Starlette()
    app.mount("/static", StaticFiles(directory=_VENDOR.parent), name="static")
    client = TestClient(app)

    resp = client.get("/static/vendor/d3-sankey.min.js")
    assert resp.status_code == 200
    assert resp.content == (_VENDOR / "d3-sankey.min.js").read_bytes()
