"""Business logic for `query_data` — the only tool that accepts raw SQL.

Every call runs the full validation pipeline in security/sql_validator.py
before anything is executed; nothing here trusts the shape of the incoming
string on its own.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.mcp_servers.db.config.settings import Settings
from backend.mcp_servers.db.database.connection import Database
from backend.mcp_servers.db.database.executor import execute_select
from backend.mcp_servers.db.models.queries import QueryDataRequest
from backend.mcp_servers.db.security.permissions import PermissionsConfig
from backend.mcp_servers.db.security.sql_validator import SqlValidator

logger = logging.getLogger(__name__)


def query_data(
    request: QueryDataRequest,
    *,
    settings: Settings,
    permissions: PermissionsConfig,
    db: Database,
) -> dict[str, Any]:
    validator = SqlValidator(settings, permissions, db)
    validated = validator.validate(request.sql, request.parameters)

    result = execute_select(
        db,
        validated.execution_sql,
        request.parameters,
        timeout_seconds=settings.query_timeout_seconds,
        max_rows=settings.max_query_rows,
        max_result_bytes=settings.max_result_bytes,
    )

    logger.info(
        "[QUERY] tables=%s rows=%d truncated=%s time_ms=%.1f",
        validated.tables, len(result.rows), result.truncated, result.execution_time_ms,
    )

    return {
        "success": True,
        "columns": result.columns,
        "rows": result.rows,
        "row_count": len(result.rows),
        "truncated": result.truncated,
        "execution_time_ms": round(result.execution_time_ms, 2),
    }
