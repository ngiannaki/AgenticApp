"""How the Code MCP server hands generated files back.

Captured run outputs (a matplotlib plot, a CSV...) and generate_file /
generate_ui results all come back *inline*: base64 bytes plus filename and
content type, in the tool result itself. The API turns each one into a
stored, downloadable artifact and replaces the bytes with a URL before the
model or the UI ever see the result (backend/apps/api/artifacts.py). This
server therefore keeps nothing on disk between calls and needs no storage
shared with the API — which is what lets it run in its own locked-down
container.
"""

from __future__ import annotations

import base64
import mimetypes
from typing import Any


def inline_artifact(data: bytes, filename: str, content_type: str | None = None) -> dict[str, Any]:
    return {
        "filename": filename,
        "content_type": content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
        "size_bytes": len(data),
        "data_base64": base64.b64encode(data).decode("ascii"),
    }
