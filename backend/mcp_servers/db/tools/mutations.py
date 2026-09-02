"""Business logic for insert_data, update_data, delete_data.

Filters are structured dicts translated into parameterized WHERE clauses —
values are always bound as SQL parameters, never interpolated into text.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.mcp_servers.db.config.settings import Settings
from backend.mcp_servers.db.database.connection import Database
from backend.mcp_servers.db.database.executor import execute_write
from backend.mcp_servers.db.errors import UnsupportedOperationError, ValidationError
from backend.mcp_servers.db.models.mutations import (
    SUPPORTED_OPERATORS,
    DeleteDataRequest,
    FilterDict,
    InsertDataRequest,
    UpdateDataRequest,
)
from backend.mcp_servers.db.security.identifier_validator import quote_identifier, validate_identifier
from backend.mcp_servers.db.security.permissions import PermissionsConfig

logger = logging.getLogger(__name__)


def build_filter_clause(filter_dict: FilterDict, known_columns: dict[str, str]) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    for col, value in filter_dict.items():
        validate_identifier(col, kind="filter column")
        if col not in known_columns:
            raise ValidationError(f"Unknown column '{col}' in filter.")
        quoted = quote_identifier(col, kind="column")

        if isinstance(value, dict) and value:
            for op, op_value in value.items():
                if op not in SUPPORTED_OPERATORS:
                    raise ValidationError(f"Unsupported filter operator '{op}' on column '{col}'.")
                if op == "$eq":
                    clauses.append(f"{quoted} = %s"); params.append(op_value)
                elif op == "$ne":
                    clauses.append(f"{quoted} <> %s"); params.append(op_value)
                elif op == "$gt":
                    clauses.append(f"{quoted} > %s"); params.append(op_value)
                elif op == "$gte":
                    clauses.append(f"{quoted} >= %s"); params.append(op_value)
                elif op == "$lt":
                    clauses.append(f"{quoted} < %s"); params.append(op_value)
                elif op == "$lte":
                    clauses.append(f"{quoted} <= %s"); params.append(op_value)
                elif op == "$in":
                    if not isinstance(op_value, list) or not op_value:
                        raise ValidationError(f"'$in' on column '{col}' requires a non-empty list.")
                    clauses.append(f"{quoted} = ANY(%s)"); params.append(op_value)
                elif op == "$is_null":
                    clauses.append(f"{quoted} IS NULL" if op_value else f"{quoted} IS NOT NULL")
        elif isinstance(value, dict):
            raise ValidationError(f"Empty filter operator object for column '{col}'.")
        else:
            clauses.append(f"{quoted} = %s")
            params.append(value)

    return clauses, params


def insert_data(
    request: InsertDataRequest,
    *,
    settings: Settings,
    permissions: PermissionsConfig,
    db: Database,
) -> dict[str, Any]:
    table = validate_identifier(request.table, kind="table")
    permissions.check(table, "insert")

    if len(request.records) > settings.max_batch_insert_records:
        raise ValidationError(
            f"Batch insert of {len(request.records)} records exceeds the limit of "
            f"{settings.max_batch_insert_records}."
        )

    columns = list(request.records[0].keys())
    if not columns:
        raise ValidationError("Each record must contain at least one column.")
    for column in columns:
        validate_identifier(column, kind="column")

    db.validate_columns(table, columns)
    permissions.check_columns(table, columns, operation="insert")

    for i, record in enumerate(request.records):
        if set(record.keys()) != set(columns):
            raise ValidationError(
                f"Record at index {i} has a different set of columns than the first record; "
                "batch inserts require every record to use the same columns."
            )

    quoted_table = quote_identifier(table, kind="table")
    quoted_columns = ", ".join(quote_identifier(c, kind="column") for c in columns)
    row_placeholder = "(" + ", ".join(["%s"] * len(columns)) + ")"
    all_placeholders = ", ".join([row_placeholder] * len(request.records))

    sql = f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES {all_placeholders}"
    if request.returning:
        sql += " RETURNING *"

    params: list[Any] = []
    for record in request.records:
        params.extend(record[c] for c in columns)

    affected, rows = execute_write(
        db, sql, params,
        timeout_seconds=settings.mutation_timeout_seconds,
        fetch_results=request.returning,
    )

    logger.info("[MUTATION] insert_data table=%s inserted=%d", table, affected)

    result: dict[str, Any] = {"success": True, "table": table, "inserted_count": affected}
    if request.returning:
        result["records"] = rows
    return result


def update_data(
    request: UpdateDataRequest,
    *,
    settings: Settings,
    permissions: PermissionsConfig,
    db: Database,
) -> dict[str, Any]:
    table = validate_identifier(request.table, kind="table")
    permissions.check(table, "update")

    if not request.filter and not request.allow_full_table_update:
        raise ValidationError(
            "update_data requires a filter. To update every row, pass allow_full_table_update=true explicitly."
        )

    known_columns = db.get_columns(table)

    change_columns = list(request.changes.keys())
    for column in change_columns:
        validate_identifier(column, kind="column")
    db.validate_columns(table, change_columns)
    permissions.check_columns(table, change_columns, operation="update")

    set_clause = ", ".join(f"{quote_identifier(c, kind='column')} = %s" for c in change_columns)
    set_params = [request.changes[c] for c in change_columns]

    where_clauses, where_params = build_filter_clause(request.filter, known_columns) if request.filter else ([], [])

    sql = f"UPDATE {quote_identifier(table, kind='table')} SET {set_clause}"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    if request.returning:
        sql += " RETURNING *"

    affected, rows = execute_write(
        db, sql, set_params + where_params,
        timeout_seconds=settings.mutation_timeout_seconds,
        fetch_results=request.returning,
    )

    logger.info("[MUTATION] update_data table=%s affected=%d", table, affected)

    result: dict[str, Any] = {"success": True, "table": table, "affected_rows": affected}
    if request.returning:
        result["records"] = rows
    return result


def delete_data(
    request: DeleteDataRequest,
    *,
    settings: Settings,
    permissions: PermissionsConfig,
    db: Database,
) -> dict[str, Any]:
    if not settings.allow_delete:
        raise UnsupportedOperationError("Delete operations are disabled by server configuration.")

    table = validate_identifier(request.table, kind="table")
    permissions.check(table, "delete")

    is_full_table_delete = not request.filter
    if is_full_table_delete:
        if not (request.allow_full_table_delete and settings.allow_full_table_delete):
            raise ValidationError(
                "delete_data requires a filter. To delete every row, pass allow_full_table_delete=true "
                "and the server must be configured with DB_MCP_ALLOW_FULL_TABLE_DELETE=true."
            )
        if not request.confirm:
            raise ValidationError("Full-table delete requires confirm=true.")

    known_columns = db.get_columns(table)
    where_clauses, where_params = build_filter_clause(request.filter, known_columns) if request.filter else ([], [])

    sql = f"DELETE FROM {quote_identifier(table, kind='table')}"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)

    affected, _ = execute_write(db, sql, where_params, timeout_seconds=settings.mutation_timeout_seconds)

    logger.info("[MUTATION] delete_data table=%s deleted=%d full_table=%s", table, affected, is_full_table_delete)

    return {"success": True, "table": table, "deleted_count": affected}
