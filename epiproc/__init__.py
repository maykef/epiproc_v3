"""EpiProc v3 — self-sufficient procurement engine (engine-in-a-box).

The engine is data-free. One image; each customer is a container that mounts
its own data (invoices/, pgdata/, reports/, configs/) and its own Postgres.
The engine talks to a shared vLLM server by URL and to its own Postgres only.
"""

__version__ = "3.0.0-skeleton"
