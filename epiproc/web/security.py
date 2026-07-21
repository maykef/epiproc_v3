"""Security middleware: audit logging, rate limiting, security headers, CSRF.

Ported from v1 dashboard_app/api/security.py. Single-DB: the audit_log INSERT
drops the tenant column (there is only one tenant now), and DB access goes
through the shared epiproc.db.pool connection pool.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from urllib.parse import parse_qs

from fastapi import Request
from slowapi import Limiter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from epiproc.web.session import COOKIE_NAME, COOKIE_SECURE, session_key

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CSRF — synchronizer token (double-submit cookie pattern)
# ─────────────────────────────────────────────────────────────────────────────

CSRF_COOKIE = "ds_csrf"

_CSRF_SAFE = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
# Routes exempt from CSRF validation (pre-session or safe by other means).
# /usage/events is a non-state-changing, auth-gated analytics sink written by a
# page-exit navigator.sendBeacon() — which cannot set an X-CSRF-Token header, so
# with CSRF enforced the exit beacon was always 403'd and that data was lost.
# Forging analytics events is nuisance-only, so exempting it is an acceptable
# trade for not silently dropping page-exit telemetry.
#
# POST /login is NOT exempt: login CSRF (an attacker silently logging a victim into
# the attacker's account) is a real attack. The double-submit cookie is issued
# pre-session on the GET /login response and login.html embeds the token, so the
# form posts a matching _csrf field. /login/mfa is likewise enforced.
#
# Caveat (honest scope): pre-session the token is only a double-submit cookie value,
# not bound to any server-side secret (post-login it is HMAC'd over the session
# cookie — see _make_csrf_token). So the login-CSRF guard is only as strong as the
# integrity of the ds_csrf cookie: an attacker who can SET a cookie on our domain
# (a same-site subdomain under their control, or a network position allowing cookie
# injection) could submit a matching pair. On a single-host HTTPS deployment with no
# sibling subdomains that is not reachable; document it rather than overclaim.
_CSRF_EXEMPT = frozenset({"/health", "/usage/events"})


def _make_csrf_token(request: Request) -> str:
    """Derive CSRF token from session cookie; random token if no session."""
    session_cookie = request.cookies.get(COOKIE_NAME, "")
    if session_cookie:
        return hmac.new(session_key(), session_cookie.encode(), hashlib.sha256).hexdigest()
    existing = request.cookies.get(CSRF_COOKIE, "")
    if existing and len(existing) == 64:
        return existing
    return secrets.token_hex(32)


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = _make_csrf_token(request)
        request.state.csrf_token = token

        if request.method not in _CSRF_SAFE and request.url.path not in _CSRF_EXEMPT:
            cookie_token = request.cookies.get(CSRF_COOKIE, "")
            if not cookie_token:
                return StarletteResponse("CSRF cookie missing — reload the page.", status_code=403)

            submitted = request.headers.get("x-csrf-token", "")
            if not submitted:
                body = await request.body()  # cached in request._body; form() can still read it
                ct = request.headers.get("content-type", "")
                if "application/x-www-form-urlencoded" in ct:
                    form_data = parse_qs(body.decode("utf-8", errors="ignore"))
                    submitted = form_data.get("_csrf", [""])[0]

            if not submitted or not hmac.compare_digest(submitted, cookie_token):
                return StarletteResponse("CSRF validation failed.", status_code=403)

        response = await call_next(request)

        response.set_cookie(
            CSRF_COOKIE,
            token,
            httponly=False,  # JS must read it as fallback
            samesite="lax",
            secure=COOKIE_SECURE,
            max_age=60 * 60 * 24 * 14,
        )
        return response


# ─────────────────────────────────────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────────────────────────────────────

_INSERT_AUDIT = """
    INSERT INTO public.audit_log (username, ip, action, resource, detail, user_agent)
    VALUES (%s, %s, %s, %s, %s, %s)
"""


def audit_log(
    action: str,
    username: str = "anonymous",
    ip: str | None = None,
    resource: str | None = None,
    detail: dict | None = None,
    user_agent: str | None = None,
) -> None:
    try:
        from epiproc.db.pool import pool
        with pool().connection() as conn:
            conn.execute(
                _INSERT_AUDIT,
                (
                    username,
                    ip,
                    action,
                    resource,
                    json.dumps(detail) if detail else None,
                    user_agent,
                ),
            )
    except Exception:
        # An audit write is a compliance record — do not let a failure break the
        # request, but never swallow it silently either. Surface it to the logs so
        # a broken audit trail is detectable.
        log.exception("audit_log write failed for action=%s username=%s", action, username)


def _request_username(request: Request) -> str:
    # get_session_user() sets this on every authenticated request — no extra DB hit.
    username = getattr(request.state, "username", None)
    if username:
        return username
    return "anonymous"


# X-Forwarded-For is client-supplied and trivially spoofable unless a trusted
# reverse proxy sets it. Trust it only when explicitly told we are behind one
# (EPIPROC_TRUST_XFF); otherwise use the real socket peer so audit-logged IPs
# can't be forged by any client.
_TRUST_XFF = os.environ.get("EPIPROC_TRUST_XFF", "").strip().lower() in ("1", "true", "yes")


def _request_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if _TRUST_XFF:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()  # first hop = original client
    return peer


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting (slowapi)
# ─────────────────────────────────────────────────────────────────────────────

def _key_func(request: Request) -> str:
    username = _request_username(request)
    if username != "anonymous":
        return username
    # Pre-auth (login, password reset), key on the client IP. Use _request_ip, NOT
    # slowapi's get_remote_address: the latter always returns the socket peer, so
    # behind a reverse proxy every request would carry the proxy's IP and share one
    # bucket — a single client tripping the limit would lock everyone out. _request_ip
    # honours EPIPROC_TRUST_XFF to recover the real client IP when we're proxied.
    return _request_ip(request)


limiter = Limiter(key_func=_key_func)

RATE_REPORT_GENERATE = "5/hour"
RATE_LOGIN = "10/minute"
RATE_MFA = "10/minute"      # brute-force guard on the 6-digit TOTP second step
RATE_DASHBOARD = "60/minute"
RATE_SEARCH = "30/minute"
RATE_DEFAULT = "120/minute"


# ─────────────────────────────────────────────────────────────────────────────
# Security headers middleware
# ─────────────────────────────────────────────────────────────────────────────

_STATIC_SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def _csp(nonce: str) -> str:
    """Content-Security-Policy for this response.

    'unsafe-inline' is gone from script-src: every inline <script> now carries
    this per-request nonce, and the dashboard's inline event handlers were
    migrated to delegated listeners — so a VLM string injected into the DOM can
    no longer execute even if an escaping gap were ever reintroduced. style-src
    keeps 'unsafe-inline' (inline style="" attributes are pervasive and are not a
    script-execution vector). object-src/base-uri/form-action are locked down as
    additional hardening (no plugins, no <base> hijack of relative script URLs,
    forms may only post same-origin).

    No CDN origin is whitelisted: chart.js/d3/d3-sankey are vendored under
    /static/vendor and served same-origin, so script-src stays at 'self' — a
    compromised third-party CDN can't inject into the nonce-protected page.
    """
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self'"
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Generate the nonce BEFORE the route runs so the page builder can stamp
        # it onto every inline <script>; the matching CSP header is set below.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        for header, value in _STATIC_SECURITY_HEADERS.items():
            response.headers[header] = value
        response.headers["Content-Security-Policy"] = _csp(nonce)
        return response


# ─────────────────────────────────────────────────────────────────────────────
# Audit middleware — logs actions based on route patterns
# ─────────────────────────────────────────────────────────────────────────────

# Audit log records security/compliance events only. Routine page views and
# search queries belong in usage_events (product analytics), not here.
# login, logout, and login_failed are written directly from login.py.


def _classify_action(method: str, path: str, status: int) -> str | None:
    if method == "POST" and path.startswith("/reports/generate"):
        return "report_generate"
    if method == "DELETE" and path.startswith("/reports/"):
        return "report_delete"
    if method == "GET" and path.startswith("/reports/") and path.endswith("/pdf"):
        return "report_download"
    return None


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        action = _classify_action(request.method, request.url.path, response.status_code)
        if action:
            username = _request_username(request)
            ip = _request_ip(request)
            ua = request.headers.get("user-agent", "")
            resource = request.url.path
            audit_log(
                action=action,
                username=username,
                ip=ip,
                resource=resource,
                user_agent=ua,
            )
        return response
