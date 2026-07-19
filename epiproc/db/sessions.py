"""Session CRUD (server-side session store).

Ported from v1 dashboard_app/api/db.py. Single-DB: no `SET search_path`.
"""
from __future__ import annotations

import re as _re
import uuid as _uuid
from datetime import timedelta as _td

from epiproc.db.pool import pool

_UUID_RE = _re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    _re.IGNORECASE,
)


def create_session(
    user_id: int,
    ip: str = "",
    user_agent: str = "",
    duration_days: int = 14,
) -> str:
    """Insert a new session row and return the session UUID string."""
    session_id = str(_uuid.uuid4())
    with pool().connection() as conn:
        conn.execute(
            """INSERT INTO sessions (id, user_id, expires_at, ip, user_agent)
               VALUES (%s, %s, NOW() + %s, %s::inet, %s)""",
            (session_id, user_id, _td(days=duration_days), ip or None, user_agent or None),
        )
    return session_id


def get_session_with_user(session_id: str) -> dict | None:
    """Return the user dict if the session is valid (not revoked, not expired), else None."""
    if not _UUID_RE.match(session_id):
        return None
    with pool().connection() as conn:
        row = conn.execute(
            """SELECT u.*, s.id AS session_id
               FROM sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.id = %s
                 AND s.revoked = FALSE
                 AND s.expires_at > NOW()
                 AND u.active = TRUE""",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def revoke_session(session_id: str) -> None:
    with pool().connection() as conn:
        conn.execute(
            "UPDATE sessions SET revoked = TRUE WHERE id = %s",
            (session_id,),
        )


def revoke_all_sessions(user_id: int) -> int:
    """Revoke all active sessions for a user. Returns the count revoked."""
    with pool().connection() as conn:
        rows = conn.execute(
            """UPDATE sessions SET revoked = TRUE
               WHERE user_id = %s AND revoked = FALSE
               RETURNING id""",
            (user_id,),
        ).fetchall()
    return len(rows)


def list_active_sessions(user_id: int) -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute(
            """SELECT id, created_at, expires_at, last_seen, host(ip) AS ip, user_agent
               FROM sessions
               WHERE user_id = %s AND revoked = FALSE AND expires_at > NOW()
               ORDER BY last_seen DESC NULLS LAST""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def touch_session(session_id: str) -> None:
    """Update last_seen for a session (best-effort)."""
    with pool().connection() as conn:
        conn.execute(
            "UPDATE sessions SET last_seen = NOW() WHERE id = %s",
            (session_id,),
        )
