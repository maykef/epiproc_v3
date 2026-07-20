"""Defect #1 — stored XSS in the inline-data <script> block.

`_js_json` must make VLM-extracted strings safe to embed inside a <script> tag.
The critical property: an extracted description/seller name can NOT terminate or
reopen a script context, so "</script>" must never survive verbatim.
"""
from __future__ import annotations

import json

from epiproc.web.dashboard_html import _js_json

LS = chr(0x2028)  # line separator
PS = chr(0x2029)  # paragraph separator


def test_closing_script_tag_cannot_survive():
    payload = "widget</script><script>alert(document.cookie)</script>"
    out = _js_json(payload)
    # No raw angle brackets remain, so the <script> context cannot be broken.
    assert "<" not in out
    assert ">" not in out
    assert "</script>" not in out
    assert "\\u003c/script\\u003e" in out


def test_escapes_angle_brackets_and_ampersand():
    out = _js_json({"desc": "a & b < c > d"})
    for raw in ("<", ">", "&"):
        assert raw not in out
    assert "\\u003c" in out and "\\u003e" in out and "\\u0026" in out


def test_escapes_unicode_line_separators():
    # U+2028 / U+2029 are valid JSON but break JS string literals if left raw.
    out = _js_json(f"line{LS}sep{PS}here")
    assert LS not in out and PS not in out
    assert "\\u2028" in out and "\\u2029" in out


def test_still_valid_json_and_round_trips():
    original = {"items": ["Rosa 'Avalanche'", "</script>", "a & b"], "n": 3}
    out = _js_json(original)
    # The escaped \u-sequences are still valid JSON and decode to the original.
    assert json.loads(out) == original
