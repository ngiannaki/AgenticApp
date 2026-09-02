"""Database MCP server.

Exposes six tools over FastMCP (HTTP transport, mirroring the `indexing` and
`retrieval` servers in this repo): create_table, update_table_definition,
insert_data, update_data, delete_data, query_data.

Security model, in one sentence: the agent's intent is never trusted —
structured tools generate their own SQL from validated identifiers, and the
one tool that accepts raw SQL (query_data) runs it through an AST-based
validator before anything reaches the database. See README.md.
"""

import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# Add project root to Python path to support direct script execution
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from fastmcp import FastMCP
from pydantic import ValidationError as PydanticValidationError

from backend.mcp_servers.db.config.settings import get_settings
from backend.mcp_servers.db.database.connection import get_database
from backend.mcp_servers.db.errors import ValidationError, to_error_dict
from backend.mcp_servers.db.models.mutations import DeleteDataRequest, InsertDataRequest, UpdateDataRequest
from backend.mcp_servers.db.models.queries import QueryDataRequest
from backend.mcp_servers.db.models.schema import ColumnDef, CreateTableRequest, IndexDef, SchemaOperation, UpdateTableDefinitionRequest
from backend.mcp_servers.db.security.permissions import PermissionsConfig
from backend.mcp_servers.db.tools.introspection import describe_table as _describe_table
from backend.mcp_servers.db.tools.introspection import list_tables as _list_tables
from backend.mcp_servers.db.tools.mutations import delete_data as _delete_data
from backend.mcp_servers.db.tools.mutations import insert_data as _insert_data
from backend.mcp_servers.db.tools.mutations import update_data as _update_data
from backend.mcp_servers.db.tools.query import query_data as _query_data
from backend.mcp_servers.db.tools.schema import create_table as _create_table
from backend.mcp_servers.db.tools.schema import update_table_definition as _update_table_definition

# Ensure logs directory exists
log_dir = Path(__file__).parent.parent.parent.parent / "logs"
log_dir.mkdir(exist_ok=True)

logger = logging.getLogger(__name__)
handlers = [
    logging.FileHandler(log_dir / "db_server.log", encoding="utf-8"),
    logging.StreamHandler(),
]
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
root_logger = logging.getLogger()
for handler in handlers:
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)

mcp = FastMCP("db")

settings = get_settings()
permissions = PermissionsConfig.load(settings.permissions_file)
db = get_database(settings)


def _handle(tool_name: str, fn, *args, table_hint: Optional[str] = None, **kwargs) -> dict[str, Any]:
    """Shared call wrapper: request-id tracing, timing, and error translation.

    Every tool goes through this so that (a) unexpected exceptions never leak
    internals to the agent and (b) we get consistent structured logs without
    duplicating the same try/except/log boilerplate six times.
    """

    request_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    logger.info("[%s] request_id=%s table=%s", tool_name, request_id, table_hint)

    try:
        result = fn(*args, **kwargs)
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            "[%s] request_id=%s success elapsed_ms=%.1f rows=%s affected=%s",
            tool_name, request_id, elapsed_ms,
            result.get("row_count"), result.get("affected_rows") or result.get("inserted_count") or result.get("deleted_count"),
        )
        return result
    except PydanticValidationError as exc:
        logger.warning("[%s] request_id=%s validation_error=%s", tool_name, request_id, exc.error_count())
        return to_error_dict(ValidationError(f"Invalid input: {exc.errors()[0]['msg'] if exc.errors() else str(exc)}"))
    except Exception as exc:  # noqa: BLE001 - translated centrally, never re-raised raw
        category = getattr(exc, "category", "database_error")
        logger.warning("[%s] request_id=%s failed category=%s error=%s", tool_name, request_id, category, exc)
        return to_error_dict(exc)


@mcp.tool
def list_tables() -> dict:
    """List tables that exist in this PostgreSQL database, with which operations
    (select/insert/update/delete/schema_change) are currently permitted on each per
    permissions.yaml. Tables with no permitted operation at all are omitted.

    Call this FIRST before create_table (to check whether a table you want already
    exists) or before insert_data/update_data/delete_data (to confirm you're allowed
    to write to it) — don't guess table names.
    """
    return _handle("list_tables", _list_tables, permissions=permissions, db=db)


@mcp.tool
def describe_table(table: str) -> dict:
    """Describe an existing table's real columns: logical type (STRING, INTEGER,
    FLOAT, BOOLEAN, or DATETIME — the same vocabulary create_table accepts), the
    underlying Postgres type, nullability, default value, and primary-key flag.
    Requires `select` permission on the table.

    Call this before insert_data/update_data on a table you didn't just create
    yourself, instead of guessing column names or types.
    """
    return _handle(
        "describe_table", _describe_table, table,
        permissions=permissions, db=db, table_hint=table,
    )


@mcp.tool
def create_table(
    table: str,
    columns: list[ColumnDef],
    indexes: Optional[list[IndexDef]] = None,
    if_exists: str = "fail",
) -> dict:
    """Create a new table in the PostgreSQL database from a structured schema definition.

    Generates SQL internally from validated column definitions — never
    accepts arbitrary DDL. Supports primary keys, nullability, defaults,
    unique constraints, foreign keys, and indexes. `if_exists` controls
    behavior when the table already exists: "fail" (default, returns an
    error) or "ignore" (no-op, never overwrites an existing table).

    Each column's `type` MUST be one of exactly these logical type names
    (not SQL/Postgres type names — no TEXT, VARCHAR, REAL, INT, etc.):
      STRING    -> Postgres TEXT
      INTEGER   -> Postgres INTEGER
      FLOAT     -> Postgres DOUBLE PRECISION
      BOOLEAN   -> Postgres BOOLEAN
      DATETIME  -> Postgres TIMESTAMP
    The table name goes in the `table` argument (not `table_name`).
    """

    request = CreateTableRequest(table=table, columns=columns, indexes=indexes or [], if_exists=if_exists)
    return _handle(
        "create_table", _create_table, request,
        settings=settings, permissions=permissions, db=db, table_hint=table,
    )


@mcp.tool
def update_table_definition(table: str, operations: list[SchemaOperation]) -> dict:
    """Modify an existing table's schema using structured operations.

    Supported operations: add_column, drop_column, rename_column,
    alter_column_type, alter_column_nullable, add_unique_constraint,
    add_foreign_key, drop_constraint, add_index, drop_index, rename_table.
    Destructive operations (drop_column, alter_column_type, drop_constraint,
    drop_index) require `confirm: true` on the operation AND the server must
    have destructive schema operations enabled. All operations for one call
    are applied atomically — if any fails, none are kept. Arbitrary ALTER
    TABLE SQL is never accepted.
    """

    request = UpdateTableDefinitionRequest(table=table, operations=operations)
    return _handle(
        "update_table_definition", _update_table_definition, request,
        settings=settings, permissions=permissions, db=db, table_hint=table,
    )


@mcp.tool
def insert_data(table: str, records: list[dict], returning: bool = True) -> dict:
    """Insert one or more records into a table using parameterized SQL.

    `records` is a list of `{"column": value}` dicts; every record must use
    the same set of columns (batch insert). Values are always bound as SQL
    parameters — never interpolated into SQL text. Set `returning=false` to
    skip getting the inserted rows back.
    """

    request = InsertDataRequest(table=table, records=records, returning=returning)
    return _handle(
        "insert_data", _insert_data, request,
        settings=settings, permissions=permissions, db=db, table_hint=table,
    )


@mcp.tool
def update_data(
    table: str,
    changes: dict,
    filter: Optional[dict] = None,
    allow_full_table_update: bool = False,
    returning: bool = False,
) -> dict:
    """Update records matching a structured filter.

    `filter` maps column -> value (implicit equality) or column ->
    {"$eq"|"$ne"|"$gt"|"$gte"|"$lt"|"$lte"|"$in"|"$is_null": value}. A filter
    is required; to update every row in the table, pass
    `allow_full_table_update: true` explicitly. Never accepts arbitrary SQL.
    Returns the number of affected rows (and the updated records if
    `returning: true`).
    """

    request = UpdateDataRequest(
        table=table, filter=filter or {}, changes=changes,
        allow_full_table_update=allow_full_table_update, returning=returning,
    )
    return _handle(
        "update_data", _update_data, request,
        settings=settings, permissions=permissions, db=db, table_hint=table,
    )


@mcp.tool
def delete_data(
    table: str,
    filter: Optional[dict] = None,
    allow_full_table_delete: bool = False,
    confirm: bool = False,
) -> dict:
    """Delete records matching a structured filter.

    A filter is required by default to prevent accidentally wiping a table.
    To delete every row, pass `allow_full_table_delete: true` AND
    `confirm: true` — full-table deletes must also be enabled in server
    configuration. Never accepts arbitrary SQL. Returns the number of
    deleted rows.
    """

    request = DeleteDataRequest(
        table=table, filter=filter or {},
        allow_full_table_delete=allow_full_table_delete, confirm=confirm,
    )
    return _handle(
        "delete_data", _delete_data, request,
        settings=settings, permissions=permissions, db=db, table_hint=table,
    )


@mcp.tool
def query_data(sql: str) ->dict: #, parameters: Optional[list] = None) -> dict:
    """Run a postgres read-only SQL query (SELECT, with JOIN/WHERE/GROUP BY/HAVING/
    ORDER BY/LIMIT/OFFSET/CTEs) and return the matching rows.

    This is the only tool that accepts raw SQL, and it is still not a general
    SQL executor: every query is parsed into an AST and validated before
    execution. INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/GRANT/REVOKE,
    multiple statements, transaction control, and dangerous administrative
    functions are all rejected. Use `%s` placeholders for parameters, e.g.
    `sql="SELECT id, title FROM documents WHERE source = %s LIMIT 20"`,
    `parameters=["internal"]` — values are always bound, never interpolated.
    Results are capped in row count and byte size by server configuration;
    check the `truncated` field in the response.
    """

    request = QueryDataRequest(sql=sql, parameters=[])
    return _handle(
        "query_data", _query_data, request,
        settings=settings, permissions=permissions, db=db,
    )


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
    )
