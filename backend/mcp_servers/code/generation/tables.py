"""Shared "table" content schema for csv/xlsx generation: either a single
table (headers + rows) or, for xlsx only, multiple named sheets.

Unlike documents.py's embedded table block, cell values here keep their
real type (str/int/float/bool/None) so a spreadsheet cell ends up holding
an actual number, not text that merely looks like one.
"""

from __future__ import annotations

from typing import Any

from backend.mcp_servers.code.errors import ValidationError

_CELL_TYPES = (str, int, float, bool, type(None))
_MAX_SHEETS = 20
_MAX_ROWS = 100_000


def normalize_table_content(content: dict[str, Any], *, allow_multi_sheet: bool) -> list[dict[str, Any]]:
    """Returns a list of one or more {"name", "headers", "rows"} sheets."""

    raw_sheets = content.get("sheets")
    if raw_sheets is not None:
        if not allow_multi_sheet:
            raise ValidationError("content.sheets is only supported for xlsx — csv holds a single table.")
        if not isinstance(raw_sheets, list) or not raw_sheets:
            raise ValidationError("content.sheets must be a non-empty list.")
        if len(raw_sheets) > _MAX_SHEETS:
            raise ValidationError(f"content.sheets exceeds the {_MAX_SHEETS}-sheet limit.")
        return [_normalize_sheet(i, s) for i, s in enumerate(raw_sheets)]

    return [_normalize_sheet(0, content, default_name="Sheet1")]


def _normalize_sheet(index: int, sheet: Any, *, default_name: str | None = None) -> dict[str, Any]:
    if not isinstance(sheet, dict):
        raise ValidationError(f"content.sheets[{index}] must be an object.")

    name = sheet.get("name", default_name)
    if not isinstance(name, str) or not name.strip():
        raise ValidationError(f"content.sheets[{index}].name must be a non-empty string.")
    name = name.strip()[:31]  # Excel's own sheet-name length cap

    headers = sheet.get("headers")
    if headers is not None and not (isinstance(headers, list) and all(isinstance(h, str) for h in headers)):
        raise ValidationError(f"content.sheets[{index}].headers must be a list of strings.")

    rows = sheet.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValidationError(f"content.sheets[{index}].rows must be a non-empty list of rows.")
    if len(rows) > _MAX_ROWS:
        raise ValidationError(f"content.sheets[{index}].rows exceeds the {_MAX_ROWS}-row limit.")

    width = len(headers) if headers else (len(rows[0]) if isinstance(rows[0], list) else None)
    for i, row in enumerate(rows):
        if not isinstance(row, list):
            raise ValidationError(f"content.sheets[{index}].rows[{i}] must be a list.")
        if width is not None and len(row) != width:
            raise ValidationError(f"content.sheets[{index}].rows[{i}] has {len(row)} cell(s), expected {width}.")
        for j, cell in enumerate(row):
            if not isinstance(cell, _CELL_TYPES):
                raise ValidationError(
                    f"content.sheets[{index}].rows[{i}][{j}] must be a string, number, boolean, or null."
                )

    return {"name": name, "headers": headers, "rows": rows}
