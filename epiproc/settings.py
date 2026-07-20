"""Single settings object for the whole engine. Env prefix: EPIPROC_.

There is exactly ONE config surface. No per-tenant schema switching, no second
default that disagrees with the first (v1's db.py/security.py mismatch is gone):
one container == one customer == one database.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EPIPROC_", env_file=".env", extra="ignore")

    # Identity / branding — the only customer-specific values, supplied per container.
    institution: str = "Unnamed Institution"
    instance_name: str = "epiproc"

    # This container's own Postgres (sidecar). NOT shared across customers.
    pg_dsn: str = "host=db port=5432 dbname=epiproc user=epiproc password=epiproc"

    # The ONE external dependency: the shared vLLM GPU server, reached by URL.
    vllm_url: str = "http://host.docker.internal:8000/v1"
    vllm_model: str = "qwen3.5-122b"

    # Data root inside the container (mounted from the customer folder).
    data_dir: str = "/data"

    # Web
    port: int = 5001
    cookie_secure: bool = True          # MUST stay true in production (HTTPS)
    session_key: str = ""               # set per container; empty => generated on boot

    # Optional bearer token gating GET /metrics for Prometheus scraping. When set,
    # a scraper authenticates with `Authorization: Bearer <token>` (or X-Metrics-Token).
    # When unset, /metrics requires an admin session — it is never open by default,
    # because the gauges carry supplier names and per-supplier invoice counts.
    metrics_token: str = ""

    # Processing concurrency (mirrors v1's proven ceiling: 1 GPU job, 2 API jobs)
    gpu_slots: int = 1
    api_slots: int = 2

    # Optional Anthropic backend for reports
    anthropic_api_key: str = ""


settings = Settings()
