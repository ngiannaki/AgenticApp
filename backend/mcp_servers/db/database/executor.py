"""Executes validated, parameterized SQL and shapes the result.

`execute_select` is used only by query_data, after the query has passed the
full validation pipeline in security/sql_validator.py. It enforces the
result-size safety net (row cap + approximate byte cap) independently of
whatever LIMIT the validator already injected, since defense in depth here
is cheap and a wrapped subquery is not a substitute for a hard stop.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from backend.mcp_servers.db.database.connection import Database
from backend.mcp_servers.db.database.transactions import transaction

logger = logging.getLogger(__name__)


@dataclass
class SelectResult:
    columns: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    execution_time_ms: float = 0.0


def execute_select(
    db: Database,
    sql: str,
    parameters: list[Any],
    *,
    timeout_seconds: int,
    max_rows: int,
    max_result_bytes: int,
) -> SelectResult:
    start = time.monotonic()
    with transaction(db, statement_timeout_seconds=timeout_seconds, read_only=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, parameters)
            columns = [d.name for d in cur.description] if cur.description else []

            rows: list[dict[str, Any]] = []
            truncated = False
            byte_total = 0
            for record in cur:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                row = dict(zip(columns, record))
                byte_total += len(json.dumps(row, default=str))
                if byte_total > max_result_bytes:
                    truncated = True
                    break
                rows.append(row)

    elapsed_ms = (time.monotonic() - start) * 1000
    return SelectResult(columns=columns, rows=rows, truncated=truncated, execution_time_ms=elapsed_ms)


def execute_write(
    db: Database,
    sql: str,
    parameters: list[Any],
    *,
    timeout_seconds: int,
    fetch_results: bool = False,
) -> tuple[int, list[dict[str, Any]]]:
    """Execute one parameterized write statement inside its own transaction.

    Returns (affected_row_count, returned_rows).
    """

    with transaction(db, statement_timeout_seconds=timeout_seconds) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, parameters)
            affected = cur.rowcount
            returned: list[dict[str, Any]] = []
            if fetch_results and cur.description:
                columns = [d.name for d in cur.description]
                returned = [dict(zip(columns, record)) for record in cur.fetchall()]
    return affected, returned
