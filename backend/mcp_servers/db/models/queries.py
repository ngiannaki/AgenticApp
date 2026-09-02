"""Request/result models for `query_data`."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QueryDataRequest(BaseModel):
    sql: str
    parameters: list[Any] = Field(default_factory=list)


class QueryDataResult(BaseModel):
    success: bool = True
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    execution_time_ms: float = 0.0
