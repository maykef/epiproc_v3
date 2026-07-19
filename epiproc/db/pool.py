"""psycopg3 connection pool + migration runner.

Ported clean from v1's proven db layer, minus the tenant `SET search_path`
dance — one container has one database, so every query just uses the default
schema. The job-queue claim pattern (`FOR UPDATE SKIP LOCKED`) is unchanged.
"""
from __future__ import annotations

import pathlib

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from epiproc.settings import settings

_pool: ConnectionPool | None = None
_MIGRATIONS = pathlib.Path(__file__).parent / "migrations"


def init_pool(min_size: int = 2, max_size: int = 10) -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.pg_dsn, min_size=min_size, max_size=max_size,
            kwargs={"row_factory": dict_row}, open=True,
        )
    return _pool


def pool() -> ConnectionPool:
    return init_pool()


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def run_migrations() -> list[str]:
    """Apply every migrations/*.sql once, tracked in schema_migrations.
    Guarded by an advisory lock so concurrent container starts don't race.
    """
    applied: list[str] = []
    with psycopg.connect(settings.pg_dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(918273645)")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
            )
            done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
            for path in sorted(_MIGRATIONS.glob("*.sql")):
                if path.name in done:
                    continue
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (path.name,))
                applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(918273645)")
    return applied
