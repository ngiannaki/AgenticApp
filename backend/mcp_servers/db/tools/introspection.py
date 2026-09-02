"""Business logic for `list_tables` and `describe_table` — read-only schema
introspection, gated by the same permissions.yaml as every other tool.

These exist so an agent can discover real table/column names and types
instead of guessing them (and instead of needing raw SELECT access to
information_schema, which permissions.yaml denies by default).
"""

from __future__ import annotations

from typing import Any

from backend.mcp_servers.db.database.connection import Database
from backend.mcp_servers.db.errors import ValidationError
from backend.mcp_servers.db.security.identifier_validator import validate_identifier
from backend.mcp_servers.db.security.permissions import PermissionsConfig

# Maps Postgres' own type names back to our closed logical vocabulary
# (see models/schema.py ALLOWED_COLUMN_TYPES) so describe_table's output
# uses the same type names create_table/update_table_definition accept.
_PG_TO_LOGICAL_TYPE: dict[str, str] = {
    "text": "STRING",
    "character varying": "STRING",
    "integer": "INTEGER",
    "bigint": "INTEGER",
    "smallint": "INTEGER",
    "double precision": "FLOAT",
    "real": "FLOAT",
    "numeric": "FLOAT",
    "boolean": "BOOLEAN",
    "timestamp without time zone": "DATETIME",
    "timestamp with time zone": "DATETIME",
    "date": "DATETIME",
}


def list_tables(*, permissions: PermissionsConfig, db: Database) -> dict[str, Any]:
    tables = []
    for name in db.list_public_tables():
        perms = permissions.get_table_permissions(name)
        if not any((perms.select, perms.insert, perms.update, perms.delete, perms.schema_change)):
            continue
        tables.append({
            "table": name,
            "select": perms.select,
            "insert": perms.insert,
            "update": perms.update,
            "delete": perms.delete,
            "schema_change": perms.schema_change,
            "protected": permissions.is_protected(name),
        })

    return {"success": True, "tables": tables}


def describe_table(table: str, *, permissions: PermissionsConfig, db: Database) -> dict[str, Any]:
    table_ident = validate_identifier(table, kind="table")
    permissions.check(table_ident, "select")

    details = db.get_table_details(table_ident)
    if not details:
        raise ValidationError(f"Table '{table_ident}' does not exist.", details={"table": table_ident})

    perms = permissions.get_table_permissions(table_ident)
    deny = set(perms.deny_columns)

    columns = [
        {
            "name": col["name"],
            "type": _PG_TO_LOGICAL_TYPE.get(col["data_type"], col["data_type"]),
            "postgres_type": col["data_type"],
            "nullable": col["nullable"],
            "default": col["default"],
            "primary_key": col["primary_key"],
        }
        for col in details
        if col["name"] not in deny
    ]

    return {
        "success": True,
        "table": table_ident,
        "protected": permissions.is_protected(table_ident),
        "columns": columns,
    }
