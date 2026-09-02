"""Connection pool and schema introspection cache.

Uses psycopg 3 (sync) with a `ConnectionPool`, matching the rest of this
repo's MCP servers which are built as plain synchronous FastMCP tools.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from psycopg_pool import ConnectionPool

from backend.mcp_servers.db.config.settings import Settings
from backend.mcp_servers.db.errors import ValidationError

logger = logging.getLogger(__name__)

_TABLE_EXISTS_SQL = "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = %s"
_COLUMNS_SQL = """
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = %s
"""
_LIST_TABLES_SQL = """
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name
"""
_TABLE_DETAILS_SQL = """
    SELECT
        c.column_name,
        c.data_type,
        c.is_nullable = 'YES' AS nullable,
        c.column_default,
        EXISTS (
            SELECT 1
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = 'public'
              AND tc.table_name = %s
              AND kcu.column_name = c.column_name
        ) AS primary_key
    FROM information_schema.columns c
    WHERE c.table_schema = 'public' AND c.table_name = %s
    ORDER BY c.ordinal_position
"""


class Database:
    """Owns the connection pool and a small, invalidatable schema cache.

    The schema cache exists so `query_data` and the mutation tools can
    validate table/column references against what actually exists in the
    database (not just against the permissions file) without round-tripping
    to information_schema on every single call.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            open=True,
        )
        self._lock = threading.Lock()
        self._table_columns_cache: dict[str, dict[str, str]] = {}

    def close(self) -> None:
        self._pool.close()

    def connection(self):
        return self._pool.connection()

    def invalidate_schema_cache(self, table: Optional[str] = None) -> None:
        with self._lock:
            if table is None:
                self._table_columns_cache.clear()
            else:
                self._table_columns_cache.pop(table, None)

    def table_exists(self, table: str) -> bool:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_TABLE_EXISTS_SQL, (table,))
            return cur.fetchone() is not None

    def get_columns(self, table: str, *, force_refresh: bool = False) -> dict[str, str]:
        """Return {column_name: data_type} for `table`, using a cached copy unless refreshed."""

        with self._lock:
            cached = None if force_refresh else self._table_columns_cache.get(table)
        if cached is not None:
            return cached

        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_COLUMNS_SQL, (table,))
            rows = cur.fetchall()

        if not rows:
            raise ValidationError(f"Table '{table}' does not exist.", details={"table": table})

        columns = {name: dtype for name, dtype in rows}
        with self._lock:
            self._table_columns_cache[table] = columns
        return columns

    def list_public_tables(self) -> list[str]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_LIST_TABLES_SQL)
            return [row[0] for row in cur.fetchall()]

    def get_table_details(self, table: str) -> list[dict]:
        """Return per-column details (name, data_type, nullable, default, primary_key) for `table`."""

        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_TABLE_DETAILS_SQL, (table, table))
            rows = cur.fetchall()
        return [
            {"name": name, "data_type": data_type, "nullable": nullable, "default": default, "primary_key": pk}
            for name, data_type, nullable, default, pk in rows
        ]

    def validate_columns(self, table: str, columns: list[str], *, kind: str = "column") -> None:
        known = self.get_columns(table)
        unknown = [c for c in columns if c not in known]
        if unknown:
            raise ValidationError(
                f"Unknown {kind}(s) {unknown} on table '{table}'.",
                details={"table": table, "unknown": unknown},
            )


_db: Optional[Database] = None
_init_lock = threading.Lock()


def get_database(settings: Optional[Settings] = None) -> Database:
    global _db
    if _db is None:
        with _init_lock:
            if _db is None:
                from backend.mcp_servers.db.config.settings import get_settings

                _db = Database(settings or get_settings())
    return _db
