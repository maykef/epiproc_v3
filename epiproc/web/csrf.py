"""Single source for the inline CSRF injector.

Emits a `<meta name="csrf-token">` tag plus a script that (a) adds an
X-CSRF-Token header to every non-safe fetch() and (b) injects a hidden `_csrf`
field into every form on submit. Used by both the string-built dashboard page
(via dashboard_html) and the Jinja admin templates (registered as a global),
so the logic lives in exactly one place.
"""
from __future__ import annotations

import json


def csrf_inject_html(token: str) -> str:
    if not token:
        return ""
    t = json.dumps(token)
    return (
        f'<meta name="csrf-token" content="{token}">'
        f'<script>(function(){{var t={t};'
        f'var _f=window.fetch;window.fetch=function(url,o){{o=o||{{}};'
        f"var m=(o.method||'GET').toUpperCase();"
        f"if(m==='GET'||m==='HEAD'||m==='OPTIONS')return _f(url,o);"
        f"o.headers=Object.assign({{'X-CSRF-Token':t}},o.headers||{{}});"
        f'return _f(url,o);}};'
        f"function p(){{document.querySelectorAll('form').forEach(function(f){{"
        f"if(f.dataset.csrfDone)return;f.dataset.csrfDone='1';"
        f"f.addEventListener('submit',function(){{"
        f"if(!f.querySelector('[name=\"_csrf\"]')){{"
        f"var i=document.createElement('input');i.type='hidden';"
        f"i.name='_csrf';i.value=t;f.appendChild(i);}}}});}});}}"
        f"if(document.readyState==='loading')"
        f"document.addEventListener('DOMContentLoaded',p);else p();"
        f"}})();</script>"
    )
