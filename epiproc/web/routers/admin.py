"""Admin panel — /admin/users (admin role only).

Ported from v1 dashboard_app/api/routers/admin.py. Single-DB: DB helpers now
live in epiproc.db.{users,sessions,audit,usage}; email in epiproc.web.emailer.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from typing import Annotated

from epiproc.web.auth import hash_password, generate_password, is_account_expired
from epiproc.web.security import audit_log, _request_ip
from epiproc.web.session import get_session_user
from epiproc.web.templates import templates

router = APIRouter(include_in_schema=False)


def _admin(request: Request) -> dict:
    user = get_session_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


def _audit(request: Request, me: dict, action: str, detail: dict | None = None) -> None:
    audit_log(
        action=action,
        username=me["username"],
        ip=_request_ip(request),
        resource=request.url.path,
        detail=detail,
        user_agent=request.headers.get("user-agent", ""),
    )


def _fmt_dt(dt) -> str:
    if dt is None:
        return "—"
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)[:16]


def _enrich(users: list[dict], me_username: str) -> list[dict]:
    """Add display-ready fields to each user dict for template rendering."""
    enriched = []
    for u in users:
        u = dict(u)
        u["is_self"] = u["username"] == me_username
        u["is_expired"] = bool(is_account_expired(u))
        u["last_login_str"] = _fmt_dt(u.get("last_login"))
        u["expires_at_str"] = str(u["expires_at"]) if u.get("expires_at") else "—"
        u["suppliers_str"] = ",".join(u["suppliers"]) if u.get("suppliers") else ""
        enriched.append(u)
    return enriched


def _enc(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/admin")
def admin_overview(request: Request):
    me = _admin(request)
    from epiproc.db.audit import get_admin_stats
    stats = get_admin_stats(exclude_username=me["username"])
    return templates.TemplateResponse(request, "admin/overview.html", {
        "me": me["username"],
        "stats": stats,
        "flash_msg": "",
        "flash_kind": "",
    })


@router.get("/admin/users")
def admin_users(request: Request, ok: str = "", err: str = ""):
    me = _admin(request)
    from epiproc.db.users import get_all_users
    users = _enrich(get_all_users(), me["username"])

    return templates.TemplateResponse(request, "admin/users.html", {
        "me": me["username"],
        "users": users,
        "flash_msg": ok or err,
        "flash_kind": "ok" if ok else ("err" if err else ""),
    })


@router.get("/admin/dashboard")
def admin_dashboard(request: Request, saved: str = ""):
    me = _admin(request)
    from epiproc.db.settings import DASHBOARD_TABS, get_enabled_tabs
    enabled = get_enabled_tabs()
    return templates.TemplateResponse(request, "admin/dashboard.html", {
        "me": me["username"],
        "tabs": [{"key": k, "label": lbl, "on": k in enabled} for k, lbl in DASHBOARD_TABS],
        "flash_msg": "Dashboard tabs saved." if saved else "",
        "flash_kind": "ok" if saved else "",
    })


@router.post("/admin/dashboard")
async def admin_dashboard_save(request: Request):
    me = _admin(request)
    from epiproc.db.settings import set_enabled_tabs
    form = await request.form()
    selected = form.getlist("tabs")
    set_enabled_tabs(selected)
    _audit(request, me, "admin_dashboard_tabs", {"enabled": selected})
    return RedirectResponse("/admin/dashboard?saved=1", status_code=303)


@router.post("/admin/users/new")
async def create_user(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "viewer",
    expires_at: Annotated[str, Form()] = "",
    suppliers: Annotated[str, Form()] = "",
    anthropic_reports: Annotated[str, Form()] = "",
):
    me = _admin(request)
    from epiproc.db.users import create_user as _create

    generated = False
    if not password:
        password = generate_password()
        generated = True

    sup_list = [s.strip() for s in suppliers.split(",") if s.strip()] or None
    try:
        _create(
            username=username.strip(),
            password_hash=hash_password(password),
            role=role,
            display_name=display_name.strip(),
            email=email.strip(),
            suppliers=sup_list,
            anthropic_reports=bool(anthropic_reports),
            expires_at=expires_at.strip() or None,
        )
    except Exception as exc:
        return RedirectResponse(f"/admin/users?err={_enc(str(exc))}", status_code=303)

    _audit(request, me, "admin_user_create",
           {"target_username": username.strip(), "role": role, "generated_password": generated})
    msg = f"User '{username}' created."
    if generated:
        msg += f" Auto-generated password: {password}"
    return RedirectResponse(f"/admin/users?ok={_enc(msg)}", status_code=303)


@router.post("/admin/users/{user_id}/update")
async def update_user(
    request: Request,
    user_id: int,
    display_name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "viewer",
    expires_at: Annotated[str, Form()] = "",
    suppliers: Annotated[str, Form()] = "",
    anthropic_reports: Annotated[str, Form()] = "",
):
    me = _admin(request)
    from epiproc.db.users import update_user as _update

    sup_list = [s.strip() for s in suppliers.split(",") if s.strip()] or None
    _update(
        user_id,
        display_name=display_name.strip(),
        email=email.strip(),
        role=role,
        suppliers=sup_list,
        anthropic_reports=bool(anthropic_reports),
        expires_at=expires_at.strip() or None,
    )
    _audit(request, me, "admin_user_update",
           {"target_user_id": user_id, "role": role, "expires_at": expires_at.strip() or None,
            "anthropic_reports": bool(anthropic_reports)})
    return RedirectResponse(f"/admin/users?ok={_enc('Saved.')}", status_code=303)


@router.post("/admin/users/{user_id}/password")
async def set_password(
    request: Request,
    user_id: int,
    password: Annotated[str, Form()] = "",
):
    me = _admin(request)
    from epiproc.db.users import update_user_password

    generated = not password
    if generated:
        password = generate_password()

    update_user_password(user_id, hash_password(password))
    _audit(request, me, "admin_password_set",
           {"target_user_id": user_id, "generated": generated})

    label = "Generated password" if generated else "Password updated"
    return templates.TemplateResponse(request, "admin/password.html", {
        "me": me["username"],
        "password": password,
        "label": label,
        "flash_msg": "",
        "flash_kind": "",
    })


@router.post("/admin/users/{user_id}/toggle")
async def toggle_user(request: Request, user_id: int):
    me = _admin(request)
    from epiproc.db.users import get_all_users, update_user as _update

    users = get_all_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        raise HTTPException(404, "User not found")
    if target["username"] == me["username"]:
        return RedirectResponse(f"/admin/users?err={_enc('Cannot deactivate your own account.')}", status_code=303)

    new_state = not target.get("active", True)
    _update(user_id, active=new_state)
    verb = "activated" if new_state else "deactivated"
    _audit(request, me, "admin_user_toggle",
           {"target_username": target["username"], "active": new_state})
    msg = f"{target['username']} {verb}."
    return RedirectResponse(f"/admin/users?ok={_enc(msg)}", status_code=303)


# ── MFA enrollment ─────────────────────────────────────────────────────────────

@router.get("/admin/users/{user_id}/mfa")
def mfa_page(request: Request, user_id: int):
    me = _admin(request)
    from epiproc.db.users import get_all_users
    users = get_all_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        raise HTTPException(404, "User not found")

    ctx: dict = {
        "me": me["username"],
        "target": target,
        "mfa_enabled": bool(target.get("mfa_secret")),
        "flash_msg": "",
        "flash_kind": "",
    }

    if not target.get("mfa_secret"):
        import pyotp, qrcode, io, base64 as _b64
        secret = pyotp.random_base32()
        uri = pyotp.TOTP(secret).provisioning_uri(
            name=target["username"], issuer_name="EpiProc"
        )
        qr_img = qrcode.make(uri)
        buf = io.BytesIO()
        qr_img.save(buf, format="PNG")
        ctx["qr_data"] = _b64.b64encode(buf.getvalue()).decode()
        ctx["secret"] = secret

    return templates.TemplateResponse(request, "admin/mfa.html", ctx)


@router.post("/admin/users/{user_id}/mfa/enroll")
async def mfa_enroll(
    request: Request,
    user_id: int,
    secret: Annotated[str, Form()],
    code: Annotated[str, Form()],
):
    me = _admin(request)
    from epiproc.web.auth import verify_totp
    from epiproc.db.users import set_mfa_secret

    if not verify_totp(secret, code):
        return RedirectResponse(
            f"/admin/users?err={_enc('Invalid code — MFA not enabled.')}",
            status_code=303,
        )
    set_mfa_secret(user_id, secret)
    _audit(request, me, "admin_mfa_enroll", {"target_user_id": user_id})
    return RedirectResponse(
        f"/admin/users?ok={_enc('MFA enabled.')}",
        status_code=303,
    )


@router.post("/admin/users/{user_id}/mfa/revoke")
async def mfa_revoke(request: Request, user_id: int):
    me = _admin(request)
    from epiproc.db.users import set_mfa_secret, get_all_users
    users = get_all_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        raise HTTPException(404, "User not found")
    set_mfa_secret(user_id, None)
    _audit(request, me, "admin_mfa_revoke",
           {"target_user_id": user_id, "target_username": target["username"]})
    name = target["username"]
    return RedirectResponse(
        f"/admin/users?ok={_enc(f'MFA removed for {name}.')}",
        status_code=303,
    )


@router.get("/admin/users/{user_id}/sessions")
def sessions_page(request: Request, user_id: int):
    me = _admin(request)
    from epiproc.db.users import get_all_users
    from epiproc.db.sessions import list_active_sessions
    users = get_all_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        raise HTTPException(404, "User not found")
    sessions = list_active_sessions(user_id)
    return templates.TemplateResponse(request, "admin/sessions.html", {
        "me": me["username"],
        "target": target,
        "sessions": sessions,
        "flash_msg": "",
        "flash_kind": "",
    })


@router.post("/admin/users/{user_id}/sessions/revoke-all")
async def revoke_all_user_sessions(request: Request, user_id: int):
    me = _admin(request)
    from epiproc.db.users import get_all_users
    from epiproc.db.sessions import revoke_all_sessions
    users = get_all_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        raise HTTPException(404, "User not found")
    count = revoke_all_sessions(user_id)
    _audit(request, me, "admin_sessions_revoke",
           {"target_user_id": user_id, "target_username": target["username"], "count": count})
    name = target["username"]
    return RedirectResponse(
        f"/admin/users?ok={_enc(f'{count} session(s) revoked for {name}.')}",
        status_code=303,
    )


@router.get("/admin/usage")
def usage_page(request: Request, days: int = 30):
    me = _admin(request)
    from epiproc.db.usage import get_usage_stats
    stats = get_usage_stats(days=days)
    return templates.TemplateResponse(request, "admin/usage.html", {
        "me": me["username"],
        "stats": stats,
        "days": days,
        "flash_msg": "",
        "flash_kind": "",
    })


@router.get("/admin/audit")
def audit_log_page(
    request: Request,
    username: str = "",
    action: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
):
    me = _admin(request)
    from epiproc.db.audit import (
        get_audit_log,
        count_audit_log,
        get_audit_distinct_users,
        get_audit_distinct_actions,
    )

    page_size = 100
    offset = (max(page, 1) - 1) * page_size

    rows = get_audit_log(
        username=username or None,
        action=action or None,
        date_from=date_from or None,
        date_to=date_to or None,
        limit=page_size,
        offset=offset,
    )
    total = count_audit_log(
        username=username or None,
        action=action or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    import math
    page_count = max(math.ceil(total / page_size), 1)

    return templates.TemplateResponse(request, "admin/audit.html", {
        "me": me["username"],
        "rows": rows,
        "total": total,
        "page": page,
        "page_count": page_count,
        "page_size": page_size,
        "offset": offset,
        "distinct_users": get_audit_distinct_users(),
        "distinct_actions": get_audit_distinct_actions(),
        "f_username": username,
        "f_action": action,
        "f_date_from": date_from,
        "f_date_to": date_to,
        "flash_msg": "",
        "flash_kind": "",
    })


@router.post("/admin/users/{user_id}/invite")
async def send_invite(request: Request, user_id: int):
    """Generate an invite token and send a password-set email to the user."""
    me = _admin(request)
    from epiproc.db.users import create_invite_token, get_all_users
    from epiproc.web.emailer import send_invite as _send

    users = get_all_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        raise HTTPException(404, "User not found")

    email = target.get("email") or ""
    if not email:
        return RedirectResponse(
            f"/admin/users?err={_enc('No email address on record for ' + target['username'] + '.')}",
            status_code=303,
        )

    token = create_invite_token(user_id)
    try:
        _send(to=email, username=target["username"], token=token)
    except Exception as exc:
        return RedirectResponse(
            f"/admin/users?err={_enc(f'Email failed: {exc}')}",
            status_code=303,
        )

    _audit(request, me, "admin_invite_sent",
           {"target_user_id": user_id, "target_username": target["username"], "email": email})
    return RedirectResponse(
        f"/admin/users?ok={_enc(f'Invite sent to {email}.')}",
        status_code=303,
    )
