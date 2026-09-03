"""Dispatches generate_file's {format, filename, content} to the right
writer, then persists the result the same way run_python_code's captured
artifacts are — through ArtifactStore, served back at
GET /api/files/{id}/{filename}.

Unlike run_python_code, this never executes anything: content is validated,
structured input (see documents.py / tables.py), so there's no subprocess,
no timeout, no sandbox directory involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.mcp_servers.code.artifacts import artifact_url, get_artifact_store
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
    store = get_artifact_store()
    artifact_id = store.save(data, safe_name)

    return {
        "success": True,
        "filename": safe_name,
        "url": artifact_url(settings, artifact_id, safe_name),
        "content_type": _CONTENT_TYPES[fmt],
        "size_bytes": len(data),
    }


def _finalize_filename(filename: str, fmt: str) -> str:
    name = Path(filename.strip()).name  # strip any path components a caller might have included
    if not name:
        raise ValidationError("filename must be a non-empty string.")
    ext = _EXTENSIONS[fmt]
    if not name.lower().endswith(ext):
        name = f"{name}{ext}"
    return name
