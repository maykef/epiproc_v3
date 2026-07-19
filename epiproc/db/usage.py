"""Usage / product-analytics events.

Ported from v1 dashboard_app/api/db.py. Single-DB: no `SET search_path`.
"""
from __future__ import annotations

import json as _json

from epiproc.db.pool import pool


def write_usage_events(username: str, session_id: str | None, events: list[dict]) -> None:
    """Bulk-insert client-side usage events (fire-and-forget; swallows errors)."""
    if not events:
        return
    try:
        with pool().connection() as conn:
            conn.cursor().executemany(
                """INSERT INTO usage_events
                       (ts, username, session_id, event, supplier, detail)
                   VALUES (
                       COALESCE(%s::timestamptz, NOW()),
                       %s, %s, %s, %s, %s
                   )""",
                [
                    (
                        e.get("ts_iso"),
                        username,
                        session_id,
                        e.get("event", "unknown"),
                        e.get("supplier") or None,
                        _json.dumps(e.get("detail")) if e.get("detail") else None,
                    )
                    for e in events
                    if isinstance(e, dict) and e.get("event")
                ],
            )
    except Exception:
        pass


def get_usage_stats(days: int = 30) -> dict:
    """Aggregate usage data for the admin analytics page."""
    interval = f"{int(days)} days"
    with pool().connection() as conn:
        tab_stats = conn.execute(
            """SELECT
                   detail->>'tab' AS tab,
                   COUNT(*)       AS views,
                   ROUND(
                       (AVG((detail->>'prev_duration_ms')::numeric) / 1000)::numeric,
                       1
                   ) AS avg_seconds
               FROM usage_events
               WHERE event = 'tab_view'
                 AND ts > NOW() - %s::interval
                 AND detail->>'tab' IS NOT NULL
               GROUP BY detail->>'tab'
               ORDER BY views DESC""",
            (interval,),
        ).fetchall()

        feature_stats = conn.execute(
            """SELECT event, COUNT(*) AS n
               FROM usage_events
               WHERE ts > NOW() - %s::interval
                 AND event != 'tab_view'
               GROUP BY event
               ORDER BY n DESC""",
            (interval,),
        ).fetchall()

        user_stats = conn.execute(
            """SELECT
                   username,
                   COUNT(*) AS total_events,
                   COUNT(*) FILTER (WHERE event = 'tab_view')       AS tab_views,
                   COUNT(*) FILTER (WHERE event = 'search_submit')  AS searches,
                   COUNT(*) FILTER (WHERE event = 'report_request') AS reports,
                   MAX(ts) AS last_seen
               FROM usage_events
               WHERE ts > NOW() - %s::interval
               GROUP BY username
               ORDER BY total_events DESC""",
            (interval,),
        ).fetchall()

        daily_counts = conn.execute(
            """SELECT
                   DATE(ts AT TIME ZONE 'UTC') AS day,
                   COUNT(*) AS events
               FROM usage_events
               WHERE ts > NOW() - %s::interval
               GROUP BY day
               ORDER BY day""",
            (interval,),
        ).fetchall()

        top_searches = conn.execute(
            """SELECT
                   detail->>'query' AS query,
                   COUNT(*)         AS n
               FROM usage_events
               WHERE event = 'search_submit'
                 AND ts > NOW() - %s::interval
                 AND detail->>'query' IS NOT NULL
                 AND detail->>'query' != ''
               GROUP BY query
               ORDER BY n DESC
               LIMIT 20""",
            (interval,),
        ).fetchall()

    return {
        "tab_stats":     [dict(r) for r in tab_stats],
        "feature_stats": [dict(r) for r in feature_stats],
        "user_stats":    [dict(r) for r in user_stats],
        "daily_counts":  [dict(r) for r in daily_counts],
        "top_searches":  [dict(r) for r in top_searches],
        "days":          days,
    }
