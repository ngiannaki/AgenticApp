"""Table/column/operation permission model.

Loaded from a YAML file (see `permissions.example.yaml`). This is the
*primary* authorization boundary for the whole server: every tool consults
it before generating or executing SQL, and `query_data`'s validator consults
it for every table/column an incoming SELECT touches. The agent's stated
intent is never trusted as a substitute for this check.

Default posture is deny-by-default: a table not mentioned in the config has
no permissions at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from backend.mcp_servers.db.errors import AuthorizationError

Operation = str  # "select" | "insert" | "update" | "delete" | "schema_change"


class TablePermissions(BaseModel):
    select: bool = False
    insert: bool = False
    update: bool = False
    delete: bool = False
    schema_change: bool = False
    # Columns that are never readable/writable even when the table-level
    # operation above is allowed (e.g. hide a password_hash column from SELECT).
    deny_columns: list[str] = Field(default_factory=list)


class PermissionsConfig(BaseModel):
    # Tables not present in `tables` fall back to `default` (if set), or are
    # denied everything if `default` is also unset.
    tables: dict[str, TablePermissions] = Field(default_factory=dict)
    # Fallback policy applied to any table that has no explicit entry in
    # `tables` and is not in `protected_tables`. Leave unset to keep strict
    # deny-by-default behavior for unlisted tables.
    default: Optional[TablePermissions] = None
    # Tables that back the RAG pipeline's own infrastructure (chunks,
    # embeddings, ...). These may still allow `select` per `tables` above,
    # but insert/update/delete/schema_change are hard-blocked here
    # regardless of what `tables` or `default` grants, and cannot be
    # re-enabled per call.
    protected_tables: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "PermissionsConfig":
        file_path = Path(path)
        if not file_path.exists():
            # Fail safe: no config means no permissions, not "allow everything".
            return cls()
        with file_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls.model_validate(raw)

    def _table(self, table: str) -> Optional[TablePermissions]:
        explicit = self.tables.get(table)
        if explicit is not None:
            return explicit
        if table in self.protected_tables:
            return None
        return self.default

    def is_protected(self, table: str) -> bool:
        return table in self.protected_tables

    def get_table_permissions(self, table: str) -> TablePermissions:
        """Effective permissions for `table`: explicit entry or `default`, with
        insert/update/delete/schema_change force-denied if the table is protected."""

        perms = self._table(table) or TablePermissions()
        if self.is_protected(table):
            perms = perms.model_copy(update={"insert": False, "update": False, "delete": False, "schema_change": False})
        return perms

    def check(self, table: str, operation: Operation) -> None:
        """Raise AuthorizationError unless `operation` is permitted on `table`."""

        perms = self._table(table)
        if perms is None or not getattr(perms, operation, False):
            raise AuthorizationError(
                f"Operation '{operation}' is not permitted on table '{table}'.",
                details={"table": table, "operation": operation},
            )

        if operation != "select" and self.is_protected(table):
            raise AuthorizationError(
                f"Table '{table}' is protected: '{operation}' is not permitted regardless of "
                "table-level configuration.",
                details={"table": table, "operation": operation},
            )

    def check_columns(self, table: str, columns: list[str], *, operation: Operation) -> None:
        perms = self._table(table)
        deny = set(perms.deny_columns) if perms else set()
        denied = deny.intersection(columns)
        if denied:
            raise AuthorizationError(
                f"Access to column(s) {sorted(denied)} on table '{table}' is denied for '{operation}'.",
                details={"table": table, "columns": sorted(denied), "operation": operation},
            )

    def known_tables(self) -> set[str]:
        return set(self.tables.keys())
