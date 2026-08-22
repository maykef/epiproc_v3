"""Build the full-page HTML dashboard from the template + live DB data.

Ported from v1 dashboard_app/api/dashboard_html.py. Single-DB: data comes from
epiproc.db.dashboard; institution branding comes from epiproc.settings.
"""
from __future__ import annotations

import html
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

# The template carries a neutral {{INSTITUTION}} placeholder, swapped at render
# for this container's institution name (from EPIPROC_INSTITUTION via settings).
# No demo/tenant strings are baked into the engine image.


def _csrf_inject(token: str) -> str:
    """Inline CSRF meta tag + fetch/form auto-injector. Single source in web.csrf,
    shared with the admin Jinja templates."""
    from epiproc.web.csrf import csrf_inject_html
    return csrf_inject_html(token)


def _js_json(obj) -> str:  # noqa: ANN001
    """JSON-encode a value for safe embedding inside an inline <script> block.

    json.dumps does not escape "</script>", so a VLM-extracted string (line-item
    description, seller name) containing that sequence can break out of the script
    context and execute — and the CSP allows 'unsafe-inline', so it offers no
    protection. Escaping <, >, & and the U+2028/U+2029 line separators to their
    \\u-forms keeps the payload valid JSON while making script-context breakout
    impossible.
    """
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _inline_data(data: dict) -> str:
    return (
        "<script>\n"
        f"const INVOICES    = {_js_json(data['invoices'])};\n"
        f"const ITEMS       = {_js_json(data['items'])};\n"
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
        "var m=t.getAttribute('data-page');"
        "if(m&&EN.indexOf(m)===-1)t.style.display='none';});"
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


def _costing_dashboard_script(sym: str) -> str:
    """Customer-facing 'Costing' tab: inline the read-only costing snapshot data
    and a pure-DOM renderer. Data is embedded via _js_json (script-context safe);
    all product-derived text is escaped on insert (no inline handlers — the tab is
    lazily initialised by the nav's data-init="initCosting"). Guarded so an old DB
    without the costing tables never breaks the dashboard."""
    try:
        from epiproc.db.costing import get_costing_dashboard_data, get_offer_history_data
        data = get_costing_dashboard_data()
        data.update(get_offer_history_data())
    except Exception:  # noqa: BLE001 — costing tables may be absent on an old DB
        data = {"products": [], "offers": []}
    return (
        "<script>\n"
        f"window.COSTING_DATA = {_js_json(data)};\n"
        "function initCosting(){\n"
        "  var root=document.getElementById('costing-root');\n"
        "  if(!root||root.dataset.done==='1')return; root.dataset.done='1';\n"
        "  var M={'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'};\n"
        "  function esc(s){return (s==null?'':String(s)).replace(/[&<>\"']/g,function(c){return M[c];});}\n"
        f"  function money(v){{return v==null?'\\u2014':'{sym}'+Number(v).toFixed(4);}}\n"
        "  function pct(v){return v==null?'\\u2014':(Number(v)*100).toFixed(2)+'%';}\n"
        f"  function money2(v){{return v==null?'\\u2014':'{sym}'+Number(v).toFixed(2);}}\n"
        "  function fmtDate(s){if(!s)return '\\u2014';var d=new Date(s);"
        "return isNaN(d)?String(s):d.toLocaleString();}\n"
        "  var D=(window.COSTING_DATA&&window.COSTING_DATA.products)||[];\n"
        "  if(!D.length){root.innerHTML='<div class=\"card\"><p style=\"color:var(--muted)\">"
        "No final costings yet. An administrator creates them under Admin \\u2192 Costing.</p></div>';return;}\n"
        "  var h='<div class=\"card\"><h2>Product costing (latest final version)</h2>"
        "<table><thead><tr><th>Product</th><th>EAN</th>"
        "<th>Total cost</th><th>Our Price</th><th>Our GP%</th><th>Customer GP%</th><th>Customer price</th></tr></thead><tbody>';\n"
        "  D.forEach(function(p){\n"
        "    h+='<tr><td>'+esc(p.name)+(p.version==null?' <span style=\\\"color:var(--muted)\\\">(no final costing)</span>':' <span style=\\\"color:var(--muted)\\\">v'+esc(p.version)+'</span>')+'</td>'\n"
        "      +'<td style=\\\"color:var(--muted)\\\">'+esc(p.ean||'\\u2014')+'</td>'\n"
        "      +'<td>'+money(p.total_direct_cost)+'</td>'\n"
        "      +'<td>'+money(p.our_price)+'</td>'\n"
        "      +'<td>'+pct(p.our_gp_pct)+'</td>'\n"
        "      +'<td>'+pct(p.customer_gp_pct)+'</td>'\n"
        "      +'<td>'+money(p.retail_price)+'</td></tr>';\n"
        "    var r=p.results;\n"
        "    if(r){\n"
        "      h+='<tr><td colspan=\\\"7\\\"><details><summary style=\\\"cursor:pointer;color:var(--accent)\\\">Breakdown</summary>'\n"
        "        +'<table style=\\\"margin-top:8px\\\"><tbody>'\n"
        "        +'<tr><td>Raw materials</td><td style=\\\"text-align:right\\\">'+money(r.raw_materials)+'</td></tr>'\n"
        "        +'<tr><td>Packaging &amp; equipment</td><td style=\\\"text-align:right\\\">'+money(r.pkg_equip_total)+'</td></tr>'\n"
        "        +'<tr><td>Outbound total</td><td style=\\\"text-align:right\\\">'+money(r.outbound_total)+'</td></tr>'\n"
        "        +'<tr><td>Operations total</td><td style=\\\"text-align:right\\\">'+money(r.operations_total)+'</td></tr>'\n"
        "        +'<tr style=\\\"font-weight:700\\\"><td>Total direct cost</td><td style=\\\"text-align:right\\\">'+money(r.total_direct_cost)+'</td></tr>'\n"
        "        +'<tr><td>Customer net</td><td style=\\\"text-align:right\\\">'+money(r.customer_net)+'</td></tr>'\n"
        "        +'<tr><td>Target retail</td><td style=\\\"text-align:right\\\">'+money(r.target_retail)+'</td></tr>'\n"
        "        +'<tr><td>Target selling</td><td style=\\\"text-align:right\\\">'+money(r.target_selling)+'</td></tr>'\n"
        "        +'</tbody></table></details></td></tr>';\n"
        "    }\n"
        "  });\n"
        "  h+='</tbody></table></div>';\n"
        "  var OF=(window.COSTING_DATA&&window.COSTING_DATA.offers)||[];\n"
        "  if(OF.length){\n"
        "    var o='<div class=\"card\"><h2>Offer history</h2>"
        "<p style=\"color:var(--muted);margin-top:-4px\">Each supplier offer, newest first. "
        "Open one to see the prices it recorded.</p>';\n"
        "    OF.forEach(function(f){\n"
        "      o+='<details style=\"border-top:1px solid var(--line);padding:8px 0\">'\n"
        "        +'<summary style=\"cursor:pointer\"><strong>'+esc(fmtDate(f.uploaded_at))+'</strong>'\n"
        "        +' <span style=\"color:var(--muted)\">'+esc(f.filename||'')+'</span>'\n"
        "        +' <span style=\"color:var(--muted)\">\\u2014 '+esc(f.costing_count)+' products'\n"
        "        +(f.finalised?', final':', draft')+'</span></summary>';\n"
        "      var it=f.items||[];\n"
        "      if(!it.length){o+='<p style=\"color:var(--muted)\">No prices recorded.</p>';}\n"
        "      else{\n"
        "        o+='<table style=\"margin-top:8px\"><thead><tr><th>Product</th><th>EAN</th>'\n"
        "          +'<th>Total cost</th><th>Selling</th><th>Customer price</th></tr></thead><tbody>';\n"
        "        it.forEach(function(r){\n"
        "          o+='<tr><td>'+esc(r.name)+' <span style=\"color:var(--muted)\">v'+esc(r.version)+'</span></td>'\n"
        "            +'<td style=\"color:var(--muted)\">'+esc(r.ean||'\\u2014')+'</td>'\n"
        "            +'<td>'+money2(r.total_cost)+'</td>'\n"
        "            +'<td>'+money2(r.selling_price)+'</td>'\n"
        "            +'<td>'+money2(r.retail_price)+'</td></tr>';\n"
        "        });\n"
        "        o+='</tbody></table>';\n"
        "      }\n"
        "      o+='</details>';\n"
        "    });\n"
        "    h+=o+'</div>';\n"
        "  }\n"
        "  root.innerHTML=h;\n"
        "}\n"
        "</script>"
    )


def _data_quality_banner() -> str:
    """A visible ingest-health strip on the Overview: failed PDFs (otherwise
    silently missing from the dataset) and invoices that don't reconcile."""
    from epiproc.db.dashboard import get_data_quality
    try:
        dq = get_data_quality()
    except Exception:  # noqa: BLE001 — never let the banner break the dashboard
        return ""
    fails = dq["ingest_failures"]
    warns = dq["reconciliation_warnings"]
    uncat = dq.get("uncategorised_items", 0)
    other = dq.get("other_share", 0.0)
    if not (fails or warns or uncat or other > 0.15):
        return ""
    parts = []
    if fails:
        files = ", ".join(dq["fail_files"][:5])
        more = "…" if len(dq["fail_files"]) > 5 else ""
        parts.append(f'<strong>{fails} invoice PDF(s) failed to process</strong>'
                     f'<span style="opacity:.8"> — not in the dataset: {files}{more}</span>')
    if warns:
        parts.append(f'<strong>{warns} invoice(s) don\'t reconcile</strong>'
                     '<span style="opacity:.8"> — line items ≠ invoice total</span>')
    if uncat:
        parts.append(f'<strong>{uncat} line item(s) uncategorised</strong>'
                     '<span style="opacity:.8"> — classification failed; will retry</span>')
    if other > 0.15:
        parts.append(f'<strong>{other * 100:.0f}% of spend is "Other"</strong>'
                     '<span style="opacity:.8"> — re-derive categories from data '
                     '(Admin → Dashboard)</span>')
    inner = " &nbsp;·&nbsp; ".join(parts)
    return (
        '<div style="margin:0 0 14px;padding:10px 14px;border-radius:8px;font-size:12.5px;'
        'background:rgba(255,159,67,.12);border:1px solid rgba(255,159,67,.45);'
        'color:var(--text)">⚠ ' + inner + '</div>'
    )


def _apply(template: str, subs: dict) -> str:
    for k, v in subs.items():
        template = template.replace(k, v)
    # Currency symbol is per-customer (the template hardcodes £). Applied after
    # substitutions so injected values (e.g. the header grand total) convert too;
    # the costing tab's script embeds it directly.
    from epiproc.db.settings import get_currency_symbol, get_enabled_tabs, get_price_tracker_key
    sym = get_currency_symbol()
    template = template.replace("</nav>", _LOGOUT_BTN, 1)
    template = template.replace("{{INSTITUTION}}", settings.institution)
    template = template.replace("{{DATA_QUALITY}}", _data_quality_banner())
    template = template.replace("</body>", _costing_dashboard_script(sym) + _tab_toggle_js() + "</body>", 1)
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
    # display_name derives from a VLM-extracted seller name — HTML-escape it
    # everywhere it lands in raw markup (header, KPI sub, filter option).
    disp = html.escape(data["display_name"])

    return _apply(template, {
        "{{SUP_TITLE}}":             disp,
        "{{HEADER_SUB}}":            f"{n_inv} invoices  ·  Grand total £{grand_total:,.0f}",
        "{{SUP_TAB}}":               "",
        "{{SUP_FILTER}}":            f'<option value="{html.escape(supplier)}">{disp}</option>',
        "{{N_INV}}":                 str(n_inv),
        "{{KPI_SUP_SUB}}":           f"from {disp}",
        "{{N_ITEMS}}":               str(data["n_items"]),
        "{{CATPIE_CLASS}}":          "card span2",
        "{{SUPPIE_CARD}}":           "",
        "{{SUP_TOTALS_JSON}}":       _js_json(data["sup_totals"]),
        "{{CAT_TOTALS_JSON}}":       _js_json(data["cat_totals"]),
        "{{DEPT_TOTALS_JSON}}":      _js_json(data["dept_totals"]),
        "{{CAT_DEPT_JSON}}":         _js_json(data["cat_dept"]),
        "{{MONTHLY_CAT_JSON}}":      _js_json(data["monthly_cat"]),
        "{{MONTHLY_SUP_JSON}}":      _js_json(data["monthly_sup"]),
        "{{CAT_SUPPLIER_JSON}}":     _js_json(data["cat_supplier"]),
        "{{SUP_NAMES_JSON}}":        _js_json(data["sup_names"]),
        "{{SUP_COLORS_JSON}}":       _js_json(data["sup_colors"]),
        "{{SUP_KEYS_JSON}}":         _js_json([supplier]),
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
        '<select id="global-sup" autocomplete="off" data-change="applyGlobalSupFilter">'
        '<option value="">All</option>'
        '</select>'
        '</div>'
    )
    sup_tab = '<div class="tab" data-click="showPage" data-page="suppliers">By Supplier</div>'
    suppie_card = (
        '<div class="card span2" id="suppie-card-wrap">'
        '<h2>Spend by Supplier (£ — item level)</h2>'
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
        "{{SUP_TOTALS_JSON}}":       _js_json(data["sup_totals"]),
        "{{CAT_TOTALS_JSON}}":       _js_json(data["cat_totals"]),
        "{{DEPT_TOTALS_JSON}}":      _js_json(data["dept_totals"]),
        "{{CAT_DEPT_JSON}}":         _js_json(data["cat_dept"]),
        "{{MONTHLY_CAT_JSON}}":      _js_json(data["monthly_cat"]),
        "{{MONTHLY_SUP_JSON}}":      _js_json(data["monthly_sup"]),
        "{{CAT_SUPPLIER_JSON}}":     _js_json(data["cat_supplier"]),
        "{{SUP_NAMES_JSON}}":        _js_json(data["sup_names"]),
        "{{SUP_COLORS_JSON}}":       _js_json(data["sup_colors"]),
        "{{SUP_KEYS_JSON}}":         _js_json(data["sup_keys"]),
        "{{CAT_COLOR_OVERRIDES_JSON}}": "{}",
        "{{LOAD_TIME}}":             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "{{ADMIN_LINK}}":            _ADMIN_LINK if is_admin else "",
        "{{USAGE_TRACKER_JS}}":      _USAGE_TRACKER_JS,
        "{{CSRF_INJECT}}":           _csrf_inject(csrf_token),
    })
