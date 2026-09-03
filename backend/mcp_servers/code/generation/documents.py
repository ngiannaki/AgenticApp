"""Shared "document" content schema for txt/md/docx generation.

An optional title plus an ordered list of sections, each with any mix of a
heading, paragraphs, a bullet list, a numbered list, and a table. The same
normalized shape feeds all three renderers in writers.py — validation lives
here once instead of being duplicated per format.
"""

from __future__ import annotations

from typing import Any

from backend.mcp_servers.code.errors import ValidationError

_MAX_SECTIONS = 100


def normalize_document(content: dict[str, Any]) -> dict[str, Any]:
    title = content.get("title")
    if title is not None and not isinstance(title, str):
        raise ValidationError("content.title must be a string.")

    raw_sections = content.get("sections", [])
    if not isinstance(raw_sections, list):
        raise ValidationError("content.sections must be a list.")
    if len(raw_sections) > _MAX_SECTIONS:
        raise ValidationError(f"content.sections exceeds the {_MAX_SECTIONS}-section limit.")

    sections = [_normalize_section(i, s) for i, s in enumerate(raw_sections)]

    if not title and not sections:
        raise ValidationError("content must have a title and/or at least one section.")

    return {"title": title, "sections": sections}


def _normalize_section(index: int, section: Any) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise ValidationError(f"content.sections[{index}] must be an object.")

    heading = section.get("heading")
    if heading is not None and not isinstance(heading, str):
        raise ValidationError(f"content.sections[{index}].heading must be a string.")

    level = section.get("level", 1)
    if not isinstance(level, int) or isinstance(level, bool) or not (1 <= level <= 4):
        raise ValidationError(f"content.sections[{index}].level must be an integer 1-4.")

    paragraphs = _string_list(section.get("paragraphs", []), f"content.sections[{index}].paragraphs")
    bullets = _string_list(section.get("bullets", []), f"content.sections[{index}].bullets")
    numbered = _string_list(section.get("numbered", []), f"content.sections[{index}].numbered")

    table = section.get("table")
    if table is not None:
        table = _normalize_table_block(table, f"content.sections[{index}].table")

    if not any([heading, paragraphs, bullets, numbered, table]):
        raise ValidationError(
            f"content.sections[{index}] must have at least one of heading/paragraphs/bullets/numbered/table."
        )

    return {
        "heading": heading,
        "level": level,
        "paragraphs": paragraphs,
        "bullets": bullets,
        "numbered": numbered,
        "table": table,
    }


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{path} must be a list of strings.")
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ValidationError(f"{path}[{i}] must be a string.")
    return value


def _normalize_table_block(table: Any, path: str) -> dict[str, Any]:
    """A table embedded in a document section. Cells are rendered as text
    here (unlike the standalone csv/xlsx table schema in tables.py, which
    keeps real types) since this feeds prose documents, not spreadsheets."""

    if not isinstance(table, dict):
        raise ValidationError(f"{path} must be an object with headers/rows.")

    headers = table.get("headers")
    if headers is not None and not (isinstance(headers, list) and all(isinstance(h, str) for h in headers)):
        raise ValidationError(f"{path}.headers must be a list of strings.")

    rows = table.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValidationError(f"{path}.rows must be a non-empty list of rows.")

    width = len(headers) if headers else (len(rows[0]) if isinstance(rows[0], list) else None)
    normalized_rows = []
    for i, row in enumerate(rows):
        if not isinstance(row, list):
            raise ValidationError(f"{path}.rows[{i}] must be a list.")
        if width is not None and len(row) != width:
            raise ValidationError(f"{path}.rows[{i}] has {len(row)} cell(s), expected {width}.")
        normalized_rows.append(["" if c is None else str(c) for c in row])

    return {"headers": headers, "rows": normalized_rows}
