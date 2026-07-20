"""CRUD for the `ingested_files` ledger (see migration 0004).

The folder-scanner records every PDF it processes here so it never re-extracts a
file it has already handled. Keyed by absolute path; content hash lets us skip a
byte-identical copy that arrives under a different name.
"""
from __future__ import annotations


def get(conn, path: str) -> dict | None:  # noqa: ANN001
    return conn.execute(
        "SELECT * FROM ingested_files WHERE path = %s", (path,)
    ).fetchone()


def sha_ingested(conn, sha: str, exclude_path: str) -> bool:  # noqa: ANN001
    """True if this exact content was already ingested under a different path."""
    row = conn.execute(
        "SELECT 1 FROM ingested_files "
        "WHERE sha256 = %s AND path <> %s AND result = 'ingested' LIMIT 1",
        (sha, exclude_path),
    ).fetchone()
    return row is not None


def record(conn, path: str, mtime: float, sha: str,  # noqa: ANN001
           invoice_id: int | None, result: str, message: str | None,
           attempts: int = 0) -> None:
    conn.execute(
        """INSERT INTO ingested_files (path, mtime, sha256, invoice_id, result, message, attempts)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (path) DO UPDATE SET
             mtime = EXCLUDED.mtime, sha256 = EXCLUDED.sha256,
             invoice_id = EXCLUDED.invoice_id, result = EXCLUDED.result,
             message = EXCLUDED.message, attempts = EXCLUDED.attempts, processed_at = now()""",
        (path, mtime, sha, invoice_id, result, message, attempts),
    )
    conn.commit()


def failure_count(conn) -> int:  # noqa: ANN001
    """How many files ended in a (non-transient) error — for the dashboard."""
    return conn.execute(
        "SELECT count(*) AS c FROM ingested_files WHERE result = 'error'"
    ).fetchone()["c"]
