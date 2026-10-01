"""Tiny SQL migration runner (no Alembic): applies backend/migrations/*.sql
once each, in filename order, and records them in public.schema_migrations.

Safe to call from several processes at once — the API and the indexing and
retrieval MCP servers all call it at startup, so whichever starts first
creates the schema and the others wait on the advisory lock, then find
nothing left to do.
"""
from __future__ import annotations

import logging
from pathlib import Path

import psycopg
from psycopg import sql

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_ADVISORY_LOCK_KEY = 0x4A67_4170  # arbitrary, fixed: "only one migrator at a time"


def apply_migrations(conninfo: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations. Returns the versions applied by this call."""
    applied_now: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
                " version text PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM public.schema_migrations")}
            for path in sorted(migrations_dir.glob("*.sql")):
                version = path.stem
                if version in done:
                    continue
                logger.info("Applying migration %s", version)
                with conn.transaction():
                    # No parameters -> psycopg sends this as a simple query, so
                    # a file may hold many statements.
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO public.schema_migrations (version) VALUES (%s)", (version,))
                applied_now.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
    return applied_now


def ensure_agent_role(conninfo: str, role: str, password: str) -> None:
    """Create (or re-password) the DB MCP server's login role.

    The role gets CONNECT + CREATE on the database — CREATE so the DB MCP
    server can create each user's `ws_<hash>` schema on first use — and
    nothing on the `app`/`rag` schemas, which are owned by the app role and
    not granted to anyone. Idempotent; needs a role allowed to CREATE ROLE
    (the Postgres container's POSTGRES_USER is).
    """
    with psycopg.connect(conninfo, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(verb + " ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        dbname = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(
            sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(sql.Identifier(dbname), sql.Identifier(role))
        )
        # Belt and braces: nothing in the app's own schemas, even if someone
        # granted PUBLIC access to them by hand.
        for schema in ("app", "rag"):
            conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(sql.Identifier(schema), sql.Identifier(role)))
    logger.info("DB agent role %r is ready", role)
