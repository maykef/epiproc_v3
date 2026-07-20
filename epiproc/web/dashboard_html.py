"""Build the full-page HTML dashboard from the template + live DB data.

Ported from v1 dashboard_app/api/dashboard_html.py. Single-DB: data comes from
epiproc.db.dashboard; institution branding comes from epiproc.settings.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from epiproc.db.dashboard import get_dashboard_data, get_multi_dashboard_data
from epiproc.settings import settings

_TEMPLATE = Path(__file__).parent / "templates" / "dashboard_template.html"

_USAGE_TRACKER_JS = """<script>
(function(){
  'use strict';
  var _buf=[], _tab='overview', _t0=performance.now(), _searchT=0;
  var _ENDPOINT='/usage/events', _FLUSH_MS=30000;

  function _sup(){
    var el=document.getElementById('global-sup');
    return el&&el.value?el.value:'';
  }

  function _push(event,detail){
    var d={event:event,ts_iso:new Date().toISOString(),supplier:_sup()};
    if(detail&&Object.keys(detail).length)d.detail=detail;
    _buf.push(d);
  }

  function _flush(beacon){
    if(!_buf.length)return;
    var payload=JSON.stringify(_buf.splice(0));
    if(beacon&&navigator.sendBeacon){
      navigator.sendBeacon(_ENDPOINT,new Blob([payload],{type:'application/json'}));
    }else{
      fetch(_ENDPOINT,{method:'POST',headers:{'Content-Type':'application/json'},body:payload}).catch(function(){});
    }
  }

  // ── Tab tracking ─────────────────────────────────────────────────────────
  var _origShow=window.showPage;
  window.showPage=function(id,el){
    var dur=Math.round(performance.now()-_t0);
    _push('tab_view',{tab:id,prev_tab:_tab,prev_duration_ms:dur});
    _tab=id; _t0=performance.now();
    _flush(false);
    return _origShow(id,el);
  };

  // ── Search tracking ───────────────────────────────────────────────────────
  var _origOpen=window.openSearch;
  window.openSearch=function(){
    _searchT=performance.now();
    _push('search_open',{});
    return _origOpen&&_origOpen();
  };

  var _origClose=window.closeSearch;
  window.closeSearch=function(){
    return _origClose&&_origClose();
  };

  // Track search submissions via input debounce
  document.addEventListener('DOMContentLoaded',function(){
    var inp=document.getElementById('search-input');
    if(inp){
      var _last='',_debounce=null;
      inp.addEventListener('input',function(){
        clearTimeout(_debounce);
        var q=inp.value.trim();
        _debounce=setTimeout(function(){
          if(q&&q!==_last&&q.length>2){
            _last=q;
            // result count read from status element
            var res=document.getElementById('search-status');
            var cnt=res?parseInt(res.textContent)||0:0;
            var sfSup=(document.getElementById('sf-supplier')||{}).value||'';
            _push('search_submit',{query:q,supplier_filter:sfSup});
          }
        },800);
      });
    }

    var csvBtn=document.getElementById('search-csv-btn');
    if(csvBtn){
      csvBtn.addEventListener('click',function(){
        _push('search_csv',{});
        _flush(false);
      });
    }
  });

  // ── Report generation tracking ────────────────────────────────────────────
  var _origGenReport=window.generateReportV2;
  window.generateReportV2=function(){
    try{
      var sup=(document.getElementById('rpt-sup-pick')||{}).value||'';
      var dept=(document.getElementById('rpt-dept-pick')||{}).value||'';
      var theme=typeof _theme!=='undefined'?_theme:'';
      var scope=typeof _scope!=='undefined'?_scope:'';
      var backend=typeof _backend!=='undefined'?_backend:'';
      _push('report_request',{theme:theme,scope:scope,supplier:sup,department:dept,backend:backend});
      _flush(false);
    }catch(e){}
    return _origGenReport&&_origGenReport();
  };

  // ── Invoice thumbnail view tracking ───────────────────────────────────────
  var _origThumb=window.openThumbByKey;
  if(_origThumb){
    window.openThumbByKey=function(supplier,invoiceId){
      _push('invoice_view',{invoice_id:invoiceId});
      return _origThumb(supplier,invoiceId);
    };
  }

  // ── Periodic flush + page-exit flush ─────────────────────────────────────
  setInterval(function(){_flush(false);},_FLUSH_MS);

  document.addEventListener('visibilitychange',function(){
    if(document.visibilityState==='hidden'){
      var dur=Math.round(performance.now()-_t0);
      _push('page_exit',{tab:_tab,duration_ms:dur});
      _flush(true);
    }
  });

  window.addEventListener('pagehide',function(){
    var dur=Math.round(performance.now()-_t0);
    _push('page_exit',{tab:_tab,duration_ms:dur});
    _flush(true);
  });
})();
</script>"""

_LOGOUT_BTN = (
    '<a href="/logout" style="font-size:11px;padding:5px 12px;'
    'background:var(--surf2);border:1px solid var(--border);'
    'color:var(--muted);border-radius:6px;cursor:pointer;'
    'text-decoration:none;white-space:nowrap;margin-left:6px">'
    '&#x2192; Sign out</a>\n</nav>'
)

_ADMIN_LINK = (
    '<a href="/admin/users" style="font-size:11px;padding:5px 12px;'
    'background:var(--surf2);border:1px solid var(--border);'
    'color:var(--muted);border-radius:6px;cursor:pointer;'
    'text-decoration:none;white-space:nowrap;margin-left:6px">'
    '&#x2699; Admin</a>'
)

# The template ships with placeholder branding; swap it for this container's
# institution name (from EPIPROC_INSTITUTION via settings).
_TEMPLATE_BRAND = "Hogwarts School of Witchcraft and Wizardry"


def _csrf_inject(token: str) -> str:
    """Inline CSRF meta tag + fetch/form auto-injector. Mirrors admin/base.html."""
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


def _inline_data(data: dict) -> str:
    return (
        "<script>\n"
        f"const INVOICES    = {json.dumps(data['invoices'],              ensure_ascii=False)};\n"
        f"const ITEMS       = {json.dumps(data['items'],                 ensure_ascii=False)};\n"
        f"let   SVC         = {json.dumps(data['svc'],                   ensure_ascii=False)};\n"
        f"const SVC_BY_SUP  = {json.dumps(data.get('svc_by_sup', {}),   ensure_ascii=False)};\n"
        "</script>"
    )


def _tab_toggle_js() -> str:
    """Hide nav tabs + panels the admin has switched off for this customer.

    The tab set lives in this instance's own DB (admin panel → Dashboard). If
    everything is on, this is a no-op. Applies to admins and viewers alike — the
    admin changes the set in /admin/dashboard, everyone sees the result.
    """
    from epiproc.db.settings import _ALL, get_enabled_tabs
    enabled = get_enabled_tabs()
    if set(enabled) == set(_ALL):
        return ""
    return (
        "<script>document.addEventListener('DOMContentLoaded',function(){"
        f"var EN={json.dumps(enabled)};"
        "document.querySelectorAll('nav .tab').forEach(function(t){"
        "var m=(t.getAttribute('onclick')||'').match(/showPage\\('([a-z]+)'/);"
        "if(m&&EN.indexOf(m[1])===-1)t.style.display='none';});"
        f"{json.dumps(_ALL)}.forEach(function(k){{"
        "if(EN.indexOf(k)===-1){var p=document.getElementById('page-'+k);"
        "if(p){p.style.display='none';p.classList.remove('active');}}});"
        # Overview KPI tiles tied to a tab disappear with it (e.g. Top Department).
        "var KPI={departments:'kpi-top-dept',categories:'kpi-top-cat'};"
        "Object.keys(KPI).forEach(function(k){"
        "if(EN.indexOf(k)===-1){var e=document.getElementById(KPI[k]);"
        "var tile=e&&e.closest('.kpi');if(tile)tile.style.display='none';}});"
        "var act=document.querySelector('nav .tab.active');"
        "if(!act||act.style.display==='none'){"
        "var vis=Array.prototype.filter.call(document.querySelectorAll('nav .tab'),"
        "function(t){return t.style.display!=='none';});if(vis.length)vis[0].click();}"
        # Department off => strip Department/Sub-dept columns + filters from EVERY
        # table (they leak into By Category, Invoices, etc.), re-run after each
        # tab switch since those tables render lazily.
        "if(EN.indexOf('departments')===-1){var hideDept=function(){"
        "['inv-dept','li-dept'].forEach(function(id){var e=document.getElementById(id);if(e)e.style.display='none';});"
        "document.querySelectorAll('table').forEach(function(tbl){"
        "tbl.querySelectorAll('thead th').forEach(function(th,idx){"
        "var t=th.textContent.replace(/[^a-z-]/gi,'').toLowerCase();"
        "if(t==='department'||t==='sub-dept'){th.style.display='none';"
        "tbl.querySelectorAll('tbody tr').forEach(function(tr){var c=tr.children[idx];if(c)c.style.display='none';});}});});};"
        "hideDept();var _sp=window.showPage;if(typeof _sp==='function'){"
        "window.showPage=function(){var r=_sp.apply(this,arguments);setTimeout(hideDept,0);return r;};}}"
        "});</script>"
    )


def _apply(template: str, subs: dict) -> str:
    for k, v in subs.items():
        template = template.replace(k, v)
    template = template.replace("</nav>", _LOGOUT_BTN, 1)
    template = template.replace(_TEMPLATE_BRAND, settings.institution)
    template = template.replace("</body>", _tab_toggle_js() + "</body>", 1)
    # Currency symbol is per-customer (the template hardcodes £). Applied after
    # substitutions so injected values (e.g. the header grand total) convert too.
    from epiproc.db.settings import get_currency_symbol, get_enabled_tabs, get_price_tracker_key
    sym = get_currency_symbol()
    if sym != "£":
        template = template.replace("£", sym)
    template = template.replace(
        "</head>",
        f"<script>window.PT_GROUP={json.dumps(get_price_tracker_key())};"
        f"window.EPIPROC_TABS={json.dumps(get_enabled_tabs())};</script></head>", 1,
    )
    return template


def build_dashboard_html(supplier: str, is_admin: bool = False, csrf_token: str = "") -> str:
    """Single-supplier dashboard (used by /dashboard/{supplier} for backward compat)."""
    data = get_dashboard_data(supplier)
    template = _TEMPLATE.read_text(encoding="utf-8")
    template = template.replace('<script src="{{DATA_FILE}}"></script>', _inline_data(data))

    grand_total = data["grand_total"]
    n_inv = data["n_inv"]

    return _apply(template, {
        "{{SUP_TITLE}}":             data["display_name"],
        "{{HEADER_SUB}}":            f"{n_inv} invoices  ·  Grand total £{grand_total:,.0f}",
        "{{SUP_TAB}}":               "",
        "{{SUP_FILTER}}":            f'<option value="{supplier}">{data["display_name"]}</option>',
        "{{N_INV}}":                 str(n_inv),
        "{{KPI_SUP_SUB}}":           f"from {data['display_name']}",
        "{{N_ITEMS}}":               str(data["n_items"]),
        "{{CATPIE_CLASS}}":          "card span2",
        "{{SUPPIE_CARD}}":           "",
        "{{SUP_TOTALS_JSON}}":       json.dumps(data["sup_totals"]),
        "{{CAT_TOTALS_JSON}}":       json.dumps(data["cat_totals"]),
        "{{DEPT_TOTALS_JSON}}":      json.dumps(data["dept_totals"]),
        "{{CAT_DEPT_JSON}}":         json.dumps(data["cat_dept"]),
        "{{MONTHLY_CAT_JSON}}":      json.dumps(data["monthly_cat"]),
        "{{MONTHLY_SUP_JSON}}":      json.dumps(data["monthly_sup"]),
        "{{CAT_SUPPLIER_JSON}}":     json.dumps(data["cat_supplier"]),
        "{{SUP_NAMES_JSON}}":        json.dumps(data["sup_names"]),
        "{{SUP_COLORS_JSON}}":       json.dumps(data["sup_colors"]),
        "{{SUP_KEYS_JSON}}":         json.dumps([supplier]),
        "{{CAT_COLOR_OVERRIDES_JSON}}": "{}",
        "{{LOAD_TIME}}":             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "{{ADMIN_LINK}}":            _ADMIN_LINK if is_admin else "",
        "{{USAGE_TRACKER_JS}}":      _USAGE_TRACKER_JS,
        "{{CSRF_INJECT}}":           _csrf_inject(csrf_token),
    })


def build_multi_dashboard_html(suppliers: list[str], is_admin: bool = False, csrf_token: str = "") -> str:
    """Multi-supplier unified dashboard with a supplier dropdown in the nav."""
    if len(suppliers) == 1:
        return build_dashboard_html(suppliers[0], is_admin=is_admin, csrf_token=csrf_token)

    data = get_multi_dashboard_data(suppliers)
    template = _TEMPLATE.read_text(encoding="utf-8")
    template = template.replace('<script src="{{DATA_FILE}}"></script>', _inline_data(data))

    n_inv = data["n_inv"]
    n_sup = data["n_suppliers"]
    grand = data["grand_total"]

    sup_filter = (
        '<div id="global-sup-wrap">'
        '<span>Supplier:</span>'
        '<select id="global-sup" autocomplete="off" onchange="applyGlobalSupFilter()">'
        '<option value="">All</option>'
        '</select>'
        '</div>'
    )
    sup_tab = '<div class="tab" onclick="showPage(\'suppliers\',this)">By Supplier</div>'
    suppie_card = (
        '<div class="card span2" id="suppie-card-wrap">'
        '<h2>Spend by Supplier (invoice level)</h2>'
        '<div id="c-suppie-wrap"></div>'
        '</div>'
    )

    return _apply(template, {
        "{{SUP_TITLE}}":    "EpiProc Atlas",
        "{{HEADER_SUB}}":   f"{n_inv} invoices &nbsp;·&nbsp; {n_sup} suppliers &nbsp;·&nbsp; Grand total £{grand:,.0f}",
        "{{SUP_TAB}}":      sup_tab,
        "{{SUP_FILTER}}":   sup_filter,
        "{{N_INV}}":        str(n_inv),
        "{{KPI_SUP_SUB}}":  f"{n_sup} suppliers",
        "{{N_ITEMS}}":      str(data["n_items"]),
        "{{CATPIE_CLASS}}": "card span2",
        "{{SUPPIE_CARD}}":  suppie_card,
        "{{SUP_TOTALS_JSON}}":       json.dumps(data["sup_totals"]),
        "{{CAT_TOTALS_JSON}}":       json.dumps(data["cat_totals"]),
        "{{DEPT_TOTALS_JSON}}":      json.dumps(data["dept_totals"]),
        "{{CAT_DEPT_JSON}}":         json.dumps(data["cat_dept"]),
        "{{MONTHLY_CAT_JSON}}":      json.dumps(data["monthly_cat"]),
        "{{MONTHLY_SUP_JSON}}":      json.dumps(data["monthly_sup"]),
        "{{CAT_SUPPLIER_JSON}}":     json.dumps(data["cat_supplier"]),
        "{{SUP_NAMES_JSON}}":        json.dumps(data["sup_names"]),
        "{{SUP_COLORS_JSON}}":       json.dumps(data["sup_colors"]),
        "{{SUP_KEYS_JSON}}":         json.dumps(data["sup_keys"]),
        "{{CAT_COLOR_OVERRIDES_JSON}}": "{}",
        "{{LOAD_TIME}}":             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "{{ADMIN_LINK}}":            _ADMIN_LINK if is_admin else "",
        "{{USAGE_TRACKER_JS}}":      _USAGE_TRACKER_JS,
        "{{CSRF_INJECT}}":           _csrf_inject(csrf_token),
    })
