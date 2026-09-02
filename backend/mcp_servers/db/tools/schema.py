"""Business logic for `create_table` and `update_table_definition`.

Both tools accept structured dictionaries/objects only; SQL is generated
here from validated identifiers and an allowlisted type/column vocabulary.
There is no path from client input to raw DDL text.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from backend.mcp_servers.db.config.settings import Settings
from backend.mcp_servers.db.database.connection import Database
from backend.mcp_servers.db.database.transactions import transaction
from backend.mcp_servers.db.errors import UnsupportedOperationError, ValidationError
from backend.mcp_servers.db.models.schema import (
    ALLOWED_COLUMN_TYPES,
    DESTRUCTIVE_OPERATIONS,
    ColumnDef,
    CreateTableRequest,
    UpdateTableDefinitionRequest,
)
from backend.mcp_servers.db.security.identifier_validator import quote_identifier, validate_identifier
from backend.mcp_servers.db.security.permissions import PermissionsConfig

logger = logging.getLogger(__name__)

_SAFE_DEFAULT_KEYWORDS = {
    "now()", "current_timestamp", "current_date", "current_time",
    "gen_random_uuid()", "uuid_generate_v4()", "true", "false", "null",
}
_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _render_default(default: str) -> str:
    normalized = default.strip()
    lowered = normalized.lower()
    if lowered in _SAFE_DEFAULT_KEYWORDS:
        return lowered if lowered.endswith("()") else lowered.upper()
    if _NUMERIC_RE.match(normalized):
        return normalized
    escaped = normalized.replace("'", "''")
    return f"'{escaped}'"


def render_column_ddl(col: ColumnDef) -> str:
    parts = [quote_identifier(col.name, kind="column"), ALLOWED_COLUMN_TYPES[col.type.upper()]]
    if not col.nullable:
        parts.append("NOT NULL")
    if col.unique:
        parts.append("UNIQUE")
    if col.default is not None:
        parts.append(f"DEFAULT {_render_default(col.default)}")
    return " ".join(parts)


def _render_foreign_key(constraint_name: str, column: str, fk) -> str:
    validate_identifier(constraint_name, kind="constraint name")
    return (
        f"CONSTRAINT {quote_identifier(constraint_name, kind='constraint name')} "
        f"FOREIGN KEY ({quote_identifier(column, kind='column')}) "
        f"REFERENCES {quote_identifier(fk.ref_table, kind='table')} ({quote_identifier(fk.ref_column, kind='column')}) "
        f"ON DELETE {fk.on_delete}"
    )


def create_table(
    request: CreateTableRequest,
    *,
    settings: Settings,
    permissions: PermissionsConfig,
    db: Database,
) -> dict[str, Any]:
    if not settings.allow_schema_changes:
        raise UnsupportedOperationError("Schema changes are disabled by server configuration.")

    table_ident = validate_identifier(request.table, kind="table")
    permissions.check(table_ident, "schema_change")

    if db.table_exists(table_ident):
        if request.if_exists == "ignore":
            return {"success": True, "table": table_ident, "created": False, "message": "Table already exists; no changes made."}
        raise ValidationError(f"Table '{table_ident}' already exists.", details={"table": table_ident})

    column_ddls = [render_column_ddl(c) for c in request.columns]

    pk_columns = [c.name for c in request.columns if c.primary_key]
    if pk_columns:
        quoted_pk = ", ".join(quote_identifier(c, kind="column") for c in pk_columns)
        column_ddls.append(f"PRIMARY KEY ({quoted_pk})")

    for col in request.columns:
        if col.foreign_key is not None:
            if not db.table_exists(col.foreign_key.ref_table):
                raise ValidationError(f"Foreign key references unknown table '{col.foreign_key.ref_table}'.")
            constraint_name = f"fk_{table_ident}_{col.name}"
            column_ddls.append(_render_foreign_key(constraint_name, col.name, col.foreign_key))

    quoted_table = quote_identifier(table_ident, kind="table")
    create_sql = f"CREATE TABLE {quoted_table} (\n  " + ",\n  ".join(column_ddls) + "\n)"

    index_statements: list[str] = []
    seen_index_names: set[str] = set()
    for idx in request.indexes:
        validate_identifier(idx.name, kind="index name")
        if idx.name in seen_index_names:
            raise ValidationError(f"Duplicate index name '{idx.name}'.")
        seen_index_names.add(idx.name)
        unknown_cols = [c for c in idx.columns if c not in {col.name for col in request.columns}]
        if unknown_cols:
            raise ValidationError(f"Index '{idx.name}' references unknown column(s) {unknown_cols}.")
        unique_kw = "UNIQUE " if idx.unique else ""
        quoted_cols = ", ".join(quote_identifier(c, kind="column") for c in idx.columns)
        index_statements.append(
            f"CREATE {unique_kw}INDEX {quote_identifier(idx.name, kind='index name')} ON {quoted_table} ({quoted_cols})"
        )

    with transaction(db, statement_timeout_seconds=settings.mutation_timeout_seconds) as conn:
        with conn.cursor() as cur:
            cur.execute(create_sql)
            for stmt in index_statements:
                cur.execute(stmt)

    db.invalidate_schema_cache(table_ident)
    logger.info("[SCHEMA] Created table '%s' with %d column(s), %d index(es)", table_ident, len(request.columns), len(index_statements))

    return {
        "success": True,
        "table": table_ident,
        "created": True,
        "columns": [c.name for c in request.columns],
        "indexes": [i.name for i in request.indexes],
    }


def _render_schema_operation(table_ident: str, op) -> tuple[list[str], bool]:
    """Returns (sql_statements, invalidates_table_name).

    Most operations return exactly one ALTER TABLE / CREATE|DROP INDEX
    statement; the bool flags whether the table's *name* changes (rename_table).
    """

    quoted_table = quote_identifier(table_ident, kind="table")
    kind = op.operation

    if kind in DESTRUCTIVE_OPERATIONS and not op.confirm:
        raise UnsupportedOperationError(
            f"Operation '{kind}' is destructive and requires confirm=true.",
            details={"operation": kind},
        )

    if kind == "add_column":
        return [f"ALTER TABLE {quoted_table} ADD COLUMN {render_column_ddl(op.column)}"], False

    if kind == "drop_column":
        col = validate_identifier(op.name, kind="column")
        return [f"ALTER TABLE {quoted_table} DROP COLUMN {quote_identifier(col, kind='column')}"], False

    if kind == "rename_column":
        old = validate_identifier(op.old_name, kind="column")
        new = validate_identifier(op.new_name, kind="column")
        return [
            f"ALTER TABLE {quoted_table} RENAME COLUMN {quote_identifier(old, kind='column')} "
            f"TO {quote_identifier(new, kind='column')}"
        ], False

    if kind == "alter_column_type":
        col = validate_identifier(op.name, kind="column")
        new_type = op.new_type.upper()
        if new_type not in ALLOWED_COLUMN_TYPES:
            raise ValidationError(f"Unsupported column type '{op.new_type}'.")
        rendered_type = ALLOWED_COLUMN_TYPES[new_type]
        quoted_col = quote_identifier(col, kind="column")
        return [
            f"ALTER TABLE {quoted_table} ALTER COLUMN {quoted_col} TYPE {rendered_type} "
            f"USING {quoted_col}::{rendered_type}"
        ], False

    if kind == "alter_column_nullable":
        col = quote_identifier(validate_identifier(op.name, kind="column"), kind="column")
        clause = "DROP NOT NULL" if op.nullable else "SET NOT NULL"
        return [f"ALTER TABLE {quoted_table} ALTER COLUMN {col} {clause}"], False

    if kind == "add_unique_constraint":
        name = validate_identifier(op.name, kind="constraint name")
        cols = ", ".join(quote_identifier(c, kind="column") for c in op.columns)
        return [
            f"ALTER TABLE {quoted_table} ADD CONSTRAINT {quote_identifier(name, kind='constraint name')} UNIQUE ({cols})"
        ], False

    if kind == "add_foreign_key":
        name = validate_identifier(op.name, kind="constraint name")
        col = validate_identifier(op.column, kind="column")
        return [f"ALTER TABLE {quoted_table} ADD {_render_foreign_key(name, col, op.foreign_key)}"], False

    if kind == "drop_constraint":
        name = validate_identifier(op.name, kind="constraint name")
        return [f"ALTER TABLE {quoted_table} DROP CONSTRAINT {quote_identifier(name, kind='constraint name')}"], False

    if kind == "add_index":
        name = validate_identifier(op.name, kind="index name")
        cols = ", ".join(quote_identifier(c, kind="column") for c in op.columns)
        unique_kw = "UNIQUE " if op.unique else ""
        return [f"CREATE {unique_kw}INDEX {quote_identifier(name, kind='index name')} ON {quoted_table} ({cols})"], False

    if kind == "drop_index":
        name = validate_identifier(op.name, kind="index name")
        return [f"DROP INDEX {quote_identifier(name, kind='index name')}"], False

    if kind == "rename_table":
        new_name = validate_identifier(op.new_name, kind="table")
        return [f"ALTER TABLE {quoted_table} RENAME TO {quote_identifier(new_name, kind='table')}"], True

    raise ValidationError(f"Unsupported schema operation '{kind}'.")


def update_table_definition(
    request: UpdateTableDefinitionRequest,
    *,
    settings: Settings,
    permissions: PermissionsConfig,
    db: Database,
) -> dict[str, Any]:
    if not settings.allow_schema_changes:
        raise UnsupportedOperationError("Schema changes are disabled by server configuration.")

    table_ident = validate_identifier(request.table, kind="table")
    permissions.check(table_ident, "schema_change")

    if not db.table_exists(table_ident):
        raise ValidationError(f"Table '{table_ident}' does not exist.")

    for op in request.operations:
        if op.operation in DESTRUCTIVE_OPERATIONS and not settings.allow_destructive_schema_ops:
            raise UnsupportedOperationError(
                f"Destructive schema operation '{op.operation}' is disabled by server configuration "
                "(DB_MCP_ALLOW_DESTRUCTIVE_SCHEMA_OPS).",
                details={"operation": op.operation},
            )

    applied: list[str] = []
    final_table_name = table_ident

    with transaction(db, statement_timeout_seconds=settings.mutation_timeout_seconds) as conn:
        with conn.cursor() as cur:
            for op in request.operations:
                statements, renames_table = _render_schema_operation(final_table_name, op)
                for stmt in statements:
                    cur.execute(stmt)
                applied.append(op.operation)
                if renames_table:
                    final_table_name = validate_identifier(op.new_name, kind="table")

    db.invalidate_schema_cache(table_ident)
    if final_table_name != table_ident:
        db.invalidate_schema_cache(final_table_name)

    logger.info("[SCHEMA] Applied %d operation(s) to table '%s' (now '%s')", len(applied), table_ident, final_table_name)

    return {
        "success": True,
        "table": final_table_name,
        "operations_applied": applied,
    }
