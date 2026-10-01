"""Dispatches generate_file's {format, filename, content} to the right
writer, then returns the file inline the same way run_python_code's captured
artifacts are; the API stores it and serves it at GET /api/files/{id}/{filename}.

Unlike run_python_code, this never executes anything: content is validated,
structured input (see documents.py / tables.py), so there's no subprocess,
no timeout, no sandbox directory involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.mcp_servers.code.artifacts import inline_artifact
from backend.mcp_servers.code.errors import ValidationError
from backend.mcp_servers.code.generation import writers

_EXTENSIONS = {"txt": ".txt", "md": ".md", "csv": ".csv", "xlsx": ".xlsx", "docx": ".docx"}
_CONTENT_TYPES = {
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_WRITERS = {
    "txt": writers.render_txt,
    "md": writers.render_md,
    "csv": writers.render_csv,
    "xlsx": writers.render_xlsx,
    "docx": writers.render_docx,
}


def generate_file(format: str, filename: str, content: dict[str, Any], *, settings: Any) -> dict[str, Any]:
    fmt = format.strip().lower() if isinstance(format, str) else ""
    if fmt not in _WRITERS:
        raise ValidationError(f"format must be one of {sorted(_WRITERS)}, got {format!r}.")
    if not filename or not filename.strip():
        raise ValidationError("filename must be a non-empty string.")
    if not isinstance(content, dict):
        raise ValidationError("content must be an object.")

    data = _WRITERS[fmt](content)

    if len(data) > settings.max_artifact_bytes:
        raise ValidationError(f"Generated file is {len(data)} bytes, exceeding the {settings.max_artifact_bytes}-byte limit.")

    safe_name = _finalize_filename(filename, fmt)
    # Returned inline; the API stores it and swaps in a URL (see artifacts.py).
    return {"success": True, **inline_artifact(data, safe_name, _CONTENT_TYPES[fmt])}


def _finalize_filename(filename: str, fmt: str) -> str:
    name = Path(filename.strip()).name  # strip any path components a caller might have included
    if not name:
        raise ValidationError("filename must be a non-empty string.")
    ext = _EXTENSIONS[fmt]
    if not name.lower().endswith(ext):
        name = f"{name}{ext}"
    return name
