"""Transactional email via the Resend REST API.

Required environment variables:
    RESEND_API_KEY      — Resend API key (re_...)
    RESEND_FROM_EMAIL   — Verified sender address (default: info@epiproc.co.uk)
    EPIPROC_PUBLIC_URL  — Public base URL for invite links (default: https://epiproc.co.uk)

If RESEND_API_KEY is not set, send_invite() logs to stdout and returns without
sending — safe for dev/test where DNS is not yet verified.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"
_FROM = os.environ.get("RESEND_FROM_EMAIL", "info@epiproc.co.uk")
_PUBLIC_URL = os.environ.get("EPIPROC_PUBLIC_URL", "https://epiproc.co.uk").rstrip("/")


def _api_key() -> str | None:
    return os.environ.get("RESEND_API_KEY")


def send_invite(to: str, username: str, token: str) -> None:
    """Send a password-set invite email.

    Raises RuntimeError if the Resend API returns a non-2xx status.
    Does nothing (logs a warning) if RESEND_API_KEY is not configured.
    """
    key = _api_key()
    if not key:
        log.warning(
            "RESEND_API_KEY not set — invite email not sent to %s (token: %s)", to, token
        )
        return

    reset_url = f"{_PUBLIC_URL}/reset/{token}"

    html = f"""
    <p>Hello,</p>
    <p>You have been invited to access the <strong>EpiProc</strong> invoice analytics platform
    as <strong>{username}</strong>.</p>
    <p>Click the button below to set your password. This link is valid for 48 hours and
    can only be used once.</p>
    <p style="margin: 24px 0;">
      <a href="{reset_url}"
         style="background:#1a56db;color:#fff;padding:12px 24px;border-radius:6px;
                text-decoration:none;font-weight:600;">
        Set your password
      </a>
    </p>
    <p>Or copy this link into your browser:<br>
    <code>{reset_url}</code></p>
    <p>If you did not expect this invitation, you can ignore this email.</p>
    <p>— The EpiProc team</p>
    """

    payload = {
        "from": _FROM,
        "to": [to],
        "subject": "You've been invited to EpiProc",
        "html": html,
    }

    resp = httpx.post(
        _RESEND_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )

    if not resp.is_success:
        raise RuntimeError(
            f"Resend API error {resp.status_code}: {resp.text}"
        )

    log.info("Invite email sent to %s via Resend (id=%s)", to, resp.json().get("id"))
