"""Transaction helpers and psycopg -> structured error translation.

Every write path (schema DDL, batch insert, update, delete) goes through
`transaction()` so that a failure partway through rolls back everything
rather than leaving partially-applied changes.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import Connection, sql

from backend.mcp_servers.db.errors import ConstraintError, QueryError, TimeoutError_
from backend.mcp_servers.db.database.connection import Database
from backend.mcp_servers.db.database.workspace import current_schema

logger = logging.getLogger(__name__)


def translate_db_error(exc: Exception) -> Exception:
    """Map a psycopg exception to one of our structured error categories."""

    if isinstance(exc, psycopg.errors.QueryCanceled):
        return TimeoutError_("The statement exceeded its execution time limit.")
    if isinstance(exc, (
        psycopg.errors.UniqueViolation,
        psycopg.errors.ForeignKeyViolation,
        psycopg.errors.NotNullViolation,
        psycopg.errors.CheckViolation,
        psycopg.errors.ExclusionViolation,
    )):
        return ConstraintError("The operation violates a database constraint.", details={"pg_code": exc.sqlstate})
    if isinstance(exc, psycopg.errors.UndefinedTable):
        return QueryError("Referenced table does not exist.")
    if isinstance(exc, psycopg.errors.UndefinedColumn):
        return QueryError("Referenced column does not exist.")
    if isinstance(exc, psycopg.Error):
        return QueryError("The database rejected the statement.")
    return exc


@contextmanager
def transaction(db: Database, *, statement_timeout_seconds: int, read_only: bool = False) -> Iterator[Connection]:
    """Yield a connection inside a transaction with a statement timeout set
    and search_path pinned to the caller's workspace schema.

    Commits on clean exit, rolls back and re-raises (translated) on any error.
    """

    with db.connection() as conn:
        conn.read_only = read_only
        try:
            with conn.cursor() as cur:
                # Postgres' SET command does not accept bound parameters ($1) under
                # the extended query protocol — interpolating is required here.
                # Safe because statement_timeout_seconds is a server-side int
                # (from Settings), never client-supplied text.
                timeout_ms = int(statement_timeout_seconds) * 1000
                cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                # Unqualified names resolve only in the caller's own workspace
                # (pg_catalog is always implicitly searched first by Postgres).
                cur.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(current_schema())))
            yield conn
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - translate everything, then re-raise
            conn.rollback()
            translated = translate_db_error(exc)
            if translated is exc and not isinstance(exc, psycopg.Error):
                raise
            logger.warning("Transaction rolled back: %s (db said: %s)", translated, exc)
            raise translated from exc
        finally:
            conn.read_only = False
