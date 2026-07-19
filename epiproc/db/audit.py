"""Audit-log queries + admin overview KPIs.

Ported from v1 dashboard_app/api/db.py. Single-DB: no `SET search_path`.
"""
from __future__ import annotations

from epiproc.db.pool import pool


# ─────────────────────────────────────────────────────────────────────────────
# Admin overview stats
# ─────────────────────────────────────────────────────────────────────────────

def get_admin_stats(exclude_username: str | None = None) -> dict:
    """Return all KPIs needed for the /admin overview page in one pool checkout."""
    with pool().connection() as conn:
        users = conn.execute(
            """SELECT
                   COUNT(*) FILTER (WHERE active)              AS active_users,
                   COUNT(*) FILTER (WHERE NOT active)          AS inactive_users,
                   COUNT(*) FILTER (WHERE role = 'admin' AND active) AS admin_users,
                   COUNT(*) FILTER (WHERE expires_at IS NOT NULL
                                    AND expires_at < CURRENT_DATE
                                    AND active)                AS expired_users
               FROM users"""
        ).fetchone()

        sessions = conn.execute(
            """SELECT COUNT(*) AS active_sessions
               FROM sessions
               WHERE revoked = FALSE AND expires_at > NOW()"""
        ).fetchone()

        failures_24h = conn.execute(
            """SELECT COUNT(*) AS n FROM audit_log
               WHERE action = 'login_failed' AND ts > NOW() - interval '24 hours'"""
        ).fetchone()

        failures_7d = conn.execute(
            """SELECT COUNT(*) AS n FROM audit_log
               WHERE action = 'login_failed' AND ts > NOW() - interval '7 days'"""
        ).fetchone()

        jobs = conn.execute(
            """SELECT
                   COUNT(*) FILTER (WHERE status = 'queued')   AS queued,
                   COUNT(*) FILTER (WHERE status = 'running')  AS running,
                   COUNT(*) FILTER (WHERE status = 'done'
                                    AND created_at > NOW() - interval '24 hours') AS done_24h
               FROM jobs"""
        ).fetchone()

        _excl = exclude_username or ""
        recent_audit = conn.execute(
            """SELECT ts, username, host(ip) AS ip, action, resource, user_agent
               FROM audit_log
               WHERE (%s = '' OR username != %s)
               ORDER BY ts DESC LIMIT 15""",
            (_excl, _excl),
        ).fetchall()

        recent_failures = conn.execute(
            """SELECT ts, username, host(ip) AS ip, user_agent
               FROM audit_log
               WHERE action = 'login_failed'
                 AND (%s = '' OR username != %s)
               ORDER BY ts DESC LIMIT 8""",
            (_excl, _excl),
        ).fetchall()

        recent_logins = conn.execute(
            """SELECT u.username, u.last_login, u.role
               FROM users u
               WHERE u.last_login IS NOT NULL AND u.active
                 AND (%s = '' OR u.username != %s)
               ORDER BY u.last_login DESC LIMIT 8""",
            (_excl, _excl),
        ).fetchall()

    return {
        "active_users":    int(users["active_users"]),
        "inactive_users":  int(users["inactive_users"]),
        "admin_users":     int(users["admin_users"]),
        "expired_users":   int(users["expired_users"]),
        "active_sessions": int(sessions["active_sessions"]),
        "failures_24h":    int(failures_24h["n"]),
        "failures_7d":     int(failures_7d["n"]),
        "jobs_queued":     int(jobs["queued"]),
        "jobs_running":    int(jobs["running"]),
        "jobs_done_24h":   int(jobs["done_24h"]),
        "recent_audit":    [dict(r) for r in recent_audit],
        "recent_failures": [dict(r) for r in recent_failures],
        "recent_logins":   [dict(r) for r in recent_logins],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Audit log queries
# ─────────────────────────────────────────────────────────────────────────────

def _audit_where(
    username: str | None,
    action: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list]:
    clauses, params = [], []
    if username:
        clauses.append("username = %s")
        params.append(username)
    if action:
        clauses.append("action = %s")
        params.append(action)
    if date_from:
        clauses.append("ts >= %s::date")
        params.append(date_from)
    if date_to:
        clauses.append("ts < (%s::date + interval '1 day')")
        params.append(date_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_audit_log(
    username: str | None = None,
    action: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    where, params = _audit_where(username, action, date_from, date_to)
    sql = f"""
        SELECT id, ts, username, host(ip) AS ip, action, resource, detail, user_agent
        FROM audit_log
        {where}
        ORDER BY ts DESC
        LIMIT %s OFFSET %s
    """
    with pool().connection() as conn:
        rows = conn.execute(sql, params + [limit, offset]).fetchall()
    return [dict(r) for r in rows]


def count_audit_log(
    username: str | None = None,
    action: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    where, params = _audit_where(username, action, date_from, date_to)
    sql = f"SELECT COUNT(*) AS n FROM audit_log {where}"
    with pool().connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def get_audit_distinct_users() -> list[str]:
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT username FROM audit_log ORDER BY username"
        ).fetchall()
    return [r["username"] for r in rows if r["username"]]


def get_audit_distinct_actions() -> list[str]:
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT action FROM audit_log ORDER BY action"
        ).fetchall()
    return [r["action"] for r in rows if r["action"]]
