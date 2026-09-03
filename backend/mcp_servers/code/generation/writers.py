"""Renders normalized content (documents.py / tables.py) into real file
bytes for each supported format.

"Real formatting" means actual document/spreadsheet structure — Word
heading styles and table borders via python-docx, bold shaded header rows
and auto-sized columns via openpyxl — not just text dumped into a file
with the right extension.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import openpyxl
from docx import Document
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from backend.mcp_servers.code.generation.documents import normalize_document
from backend.mcp_servers.code.generation.tables import normalize_table_content

# --- txt -------------------------------------------------------------------


def render_txt(content: dict[str, Any]) -> bytes:
    doc = normalize_document(content)
    lines: list[str] = []

    if doc["title"]:
        lines.append(doc["title"])
        lines.append("=" * len(doc["title"]))
        lines.append("")

    for section in doc["sections"]:
        if section["heading"]:
            lines.append(section["heading"])
            lines.append("-" * len(section["heading"]))
            lines.append("")
        for p in section["paragraphs"]:
            lines.append(p)
            lines.append("")
        for b in section["bullets"]:
            lines.append(f"  - {b}")
        if section["bullets"]:
            lines.append("")
        for i, n in enumerate(section["numbered"], start=1):
            lines.append(f"  {i}. {n}")
        if section["numbered"]:
            lines.append("")
        if section["table"]:
            lines.extend(_render_text_table(section["table"]))
            lines.append("")

    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _render_text_table(table: dict[str, Any]) -> list[str]:
    headers = table["headers"]
    rows = table["rows"]
    all_rows = ([headers] if headers else []) + rows
    widths = [max(len(r[c]) for r in all_rows) for c in range(len(all_rows[0]))]

    def fmt_row(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row))

    lines = []
    if headers:
        lines.append(fmt_row(headers))
        lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(fmt_row(row))
    return lines


# --- md ----------------------------------------------------------------------


def render_md(content: dict[str, Any]) -> bytes:
    doc = normalize_document(content)
    lines: list[str] = []

    # Markdown only has one heading namespace (# depth), unlike docx's separate
    # Title style — so nest section headings one level under the title, or
    # they'd render as siblings of it (two H1s) instead of children.
    depth_offset = 1 if doc["title"] else 0
    if doc["title"]:
        lines.append(f"# {doc['title']}")
        lines.append("")

    for section in doc["sections"]:
        if section["heading"]:
            level = min(section["level"] + depth_offset, 6)
            lines.append(f"{'#' * level} {section['heading']}")
            lines.append("")
        for p in section["paragraphs"]:
            lines.append(p)
            lines.append("")
        for b in section["bullets"]:
            lines.append(f"- {b}")
        if section["bullets"]:
            lines.append("")
        for i, n in enumerate(section["numbered"], start=1):
            lines.append(f"{i}. {n}")
        if section["numbered"]:
            lines.append("")
        if section["table"]:
            lines.extend(_render_md_table(section["table"]))
            lines.append("")

    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _render_md_table(table: dict[str, Any]) -> list[str]:
    headers = table["headers"] or [f"Column {i + 1}" for i in range(len(table["rows"][0]))]

    def esc(cell: str) -> str:
        return cell.replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(esc(h) for h in headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in table["rows"]:
        lines.append("| " + " | ".join(esc(c) for c in row) + " |")
    return lines


# --- docx --------------------------------------------------------------------


def render_docx(content: dict[str, Any]) -> bytes:
    doc_content = normalize_document(content)
    document = Document()

    if doc_content["title"]:
        document.add_heading(doc_content["title"], level=0)

    for section in doc_content["sections"]:
        if section["heading"]:
            document.add_heading(section["heading"], level=section["level"])
        for p in section["paragraphs"]:
            document.add_paragraph(p)
        for b in section["bullets"]:
            document.add_paragraph(b, style="List Bullet")
        for n in section["numbered"]:
            document.add_paragraph(n, style="List Number")
        if section["table"]:
            _add_docx_table(document, section["table"])

    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _add_docx_table(document: Document, table: dict[str, Any]) -> None:
    headers = table["headers"]
    rows = table["rows"]
    n_cols = len(headers) if headers else len(rows[0])

    doc_table = document.add_table(rows=0, cols=n_cols)
    doc_table.style = "Table Grid"

    if headers:
        header_cells = doc_table.add_row().cells
        for cell, text in zip(header_cells, headers):
            cell.text = text
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.bold = True

    for row in rows:
        cells = doc_table.add_row().cells
        for cell, text in zip(cells, row):
            cell.text = text


# --- csv -----------------------------------------------------------------------


def render_csv(content: dict[str, Any]) -> bytes:
    sheets = normalize_table_content(content, allow_multi_sheet=False)
    sheet = sheets[0]

    buf = io.StringIO()
    writer = csv.writer(buf)
    if sheet["headers"]:
        writer.writerow(sheet["headers"])
    for row in sheet["rows"]:
        writer.writerow(["" if c is None else c for c in row])

    # utf-8-sig: BOM so Excel opens non-ASCII text correctly instead of mangling it.
    return buf.getvalue().encode("utf-8-sig")


# --- xlsx --------------------------------------------------------------------

_HEADER_FILL = PatternFill(start_color="FFDDEBF7", end_color="FFDDEBF7", fill_type="solid")
_HEADER_FONT = Font(bold=True)


def render_xlsx(content: dict[str, Any]) -> bytes:
    sheets = normalize_table_content(content, allow_multi_sheet=True)
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)

    for sheet in sheets:
        ws = workbook.create_sheet(sheet["name"])
        _write_xlsx_sheet(ws, sheet)

    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


def _write_xlsx_sheet(ws: Any, sheet: dict[str, Any]) -> None:
    headers = sheet["headers"]
    rows = sheet["rows"]
    start_row = 1

    if headers:
        for col, text in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col, value=text)
            cell.font = _HEADER_FONT
            cell.fill = _HEADER_FILL
        ws.freeze_panes = "A2"
        start_row = 2

    for r, row in enumerate(rows, start=start_row):
        for c, value in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=value)

    n_cols = len(headers) if headers else (len(rows[0]) if rows else 0)
    width_rows = ([headers] if headers else []) + [["" if v is None else v for v in row] for row in rows]
    for col in range(1, n_cols + 1):
        widest = max((len(str(r[col - 1])) for r in width_rows), default=10)
        ws.column_dimensions[get_column_letter(col)].width = min(max(widest + 2, 8), 60)
