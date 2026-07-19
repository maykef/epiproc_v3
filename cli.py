#!/usr/bin/env python3
"""EpiProc v3 CLI — scaffold and drive customer instances (engine-in-a-box).

  epiproc new <name> <port>   scaffold a customer folder (invoices/ pgdata/ ...)
  epiproc up <name>           docker compose up -d for that instance
  epiproc down <name>         docker compose down
  epiproc process <name> [supplier]   enqueue an extract job (P2)

The engine image is data-free; each `new` creates ONE customer's data container.
"""
from __future__ import annotations

import argparse
import pathlib
import secrets

BASE = pathlib.Path("/mnt/nvme8tb/customers")


def cmd_new(args: argparse.Namespace) -> None:
    root = BASE / args.name
    for sub in ("invoices", "imports", "configs", "reports", "snapshots", "pgdata", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    env = root / ".env"
    if not env.exists():
        env.write_text(
            f"EPIPROC_INSTANCE_NAME={args.name}\n"
            f"EPIPROC_INSTITUTION={args.institution or args.name}\n"
            f"EPIPROC_PORT={args.port}\n"
            f"EPIPROC_PG_DSN=host=db port=5432 dbname=epiproc user=epiproc password={secrets.token_hex(16)}\n"
            f"EPIPROC_SESSION_KEY={secrets.token_hex(32)}\n"
            f"EPIPROC_VLLM_URL=http://host.docker.internal:8000/v1\n"
        )
    print(f"scaffolded instance at {root}")
    print("next: copy docker/docker-compose.template.yml -> compose.yml, then `epiproc up`")


def cmd_stub(args: argparse.Namespace) -> None:
    raise SystemExit(f"`{args.cmd}` — wired in P2 (needs the ingest engine)")


def main() -> None:
    p = argparse.ArgumentParser(prog="epiproc")
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="scaffold a customer instance folder")
    n.add_argument("name")
    n.add_argument("port", type=int)
    n.add_argument("--institution")
    n.set_defaults(func=cmd_new)
    for c in ("up", "down", "process"):
        s = sub.add_parser(c)
        s.set_defaults(func=cmd_stub)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
