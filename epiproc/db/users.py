"""User + invite-token CRUD.

Ported from v1 dashboard_app/api/db.py. Single-DB: no `SET search_path`, no
tenant column. The v3 users table has no `tenant` / `anthropic_reports`
columns, so those v1 arguments are accepted for call-site compatibility but
not persisted.
"""
from __future__ import annotations

import secrets

from epiproc.db.pool import pool


def get_user_by_username(username: str) -> dict | None:
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = %s AND active = TRUE",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def get_all_users() -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    return [dict(r) for r in rows]


def create_user(
    username: str,
    password_hash: str,
    role: str = "viewer",
    display_name: str = "",
    email: str = "",
    suppliers: list | None = None,
    expires_at: str | None = None,
    tenant: str | None = None,            # accepted for compat; not persisted
    anthropic_reports: bool = False,      # accepted for compat; not persisted
) -> int:
    with pool().connection() as conn:
        row = conn.execute(
            """INSERT INTO users
               (username, display_name, email, password_hash, role,
                suppliers, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (username, display_name, email, password_hash, role,
             suppliers or None, expires_at or None),
        ).fetchone()
    return row["id"]


def update_user(user_id: int, **fields) -> None:
    allowed = {"display_name", "email", "role", "suppliers", "expires_at", "active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    sets = [f"{k} = %s" for k in updates]
    vals = list(updates.values()) + [user_id]
    with pool().connection() as conn:
        conn.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE id = %s", vals
        )


def update_user_password(user_id: int, password_hash: str) -> None:
    with pool().connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (password_hash, user_id),
        )


def set_mfa_secret(user_id: int, secret: str | None) -> None:
    with pool().connection() as conn:
        conn.execute(
            "UPDATE users SET mfa_secret = %s WHERE id = %s",
            (secret, user_id),
        )


def record_last_login(user_id: int) -> None:
    try:
        with pool().connection() as conn:
            conn.execute(
                "UPDATE users SET last_login = NOW() WHERE id = %s",
                (user_id,),
            )
    except Exception:
        pass


# ── Invite tokens ──────────────────────────────────────────────────────────────

def create_invite_token(user_id: int) -> str:
    """Create a single-use 48-hour invite token for user_id. Returns the token string."""
    token = secrets.token_urlsafe(32)
    with pool().connection() as conn:
        conn.execute(
            "UPDATE invite_tokens SET used = TRUE WHERE user_id = %s AND used = FALSE",
            (user_id,),
        )
        conn.execute(
            """INSERT INTO invite_tokens (token, user_id, expires_at)
               VALUES (%s, %s, NOW() + interval '48 hours')""",
            (token, user_id),
        )
    return token


def get_invite_token(token: str) -> dict | None:
    """Return the token row if it exists, is unused, and has not expired."""
    with pool().connection() as conn:
        row = conn.execute(
            """SELECT t.token, t.user_id, t.expires_at, t.used, u.username, u.email
               FROM invite_tokens t
               JOIN users u ON u.id = t.user_id
               WHERE t.token = %s
                 AND t.used = FALSE
                 AND t.expires_at > NOW()""",
            (token,),
        ).fetchone()
    return dict(row) if row else None


def consume_invite_token(token: str, new_password_hash: str) -> bool:
    """Mark token used and set the user's password. Returns True on success."""
    with pool().connection() as conn:
        row = conn.execute(
            """UPDATE invite_tokens
               SET used = TRUE
               WHERE token = %s AND used = FALSE AND expires_at > NOW()
               RETURNING user_id""",
            (token,),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE users SET password_hash = %s, active = TRUE WHERE id = %s",
            (new_password_hash, row["user_id"]),
        )
    return True
