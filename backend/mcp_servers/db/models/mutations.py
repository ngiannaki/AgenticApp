"""Structured request models for insert_data, update_data, delete_data.

Filters are structured dicts, never SQL fragments. Supported comparison
operators are deliberately small: $eq, $ne, $gt, $gte, $lt, $lte, $in, $is_null.
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, Field

SUPPORTED_OPERATORS = {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$is_null"}

# A filter is either:
#   {"column": <scalar value>}                     -> implicit $eq
#   {"column": {"$op": value, ...}}                 -> explicit operator(s), ANDed together
FilterValue = Union[Any, dict[str, Any]]
FilterDict = dict[str, FilterValue]


class InsertDataRequest(BaseModel):
    table: str
    records: list[dict[str, Any]] = Field(min_length=1)
    returning: bool = True


class UpdateDataRequest(BaseModel):
    table: str
    filter: FilterDict = Field(default_factory=dict)
    changes: dict[str, Any] = Field(min_length=1)
    allow_full_table_update: bool = False
    returning: bool = False


class DeleteDataRequest(BaseModel):
    table: str
    filter: FilterDict = Field(default_factory=dict)
    allow_full_table_delete: bool = False
    confirm: bool = False
