"""One-shot database setup: apply migrations and create/refresh the DB MCP
server's restricted role.

    python -m backend.scripts.db_setup

The API does the same at startup, but start_mcp_servers.ps1 runs this first
so the indexing/retrieval/db servers (started before the API) find the
schema and the role already in place.
"""
from __future__ import annotations

import logging
import sys
import time

import psycopg

from backend.config import settings
from backend.core.db import apply_migrations, ensure_agent_role


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db = settings.database
    for attempt in range(1, 16):
        try:
            applied = apply_migrations(db.url)
            print(f"Migrations applied: {', '.join(applied) or 'none pending'}")
            break
        except psycopg.OperationalError as exc:
            print(f"Postgres not reachable yet (attempt {attempt}/15): {exc}")
            time.sleep(2)
    else:
        print("ERROR: could not reach Postgres at DATABASE_URL", file=sys.stderr)
        return 1

    if db.agent_role_password:
        ensure_agent_role(db.url, db.agent_role, db.agent_role_password)
        print(f"Role {db.agent_role!r} ready")
    else:
        print("DATABASE_AGENT_ROLE_PASSWORD not set — skipped creating the DB MCP role")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
