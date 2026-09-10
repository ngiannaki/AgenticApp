"""generate_ui: turns model-authored HTML/CSS/JS into a stored artifact the
frontend renders inside a sandboxed iframe.

Unlike generate_file, the input here isn't validated structured data — it's
free-form markup the model wrote itself, so there is no "correct" shape to
check beyond size limits. Nothing is executed on this server; the HTML is
saved as-is (wrapped in a minimal document skeleton if it isn't one already)
and only ever runs client-side, inside the sandboxed iframe the frontend
mounts it in — see ChatWindow.tsx's UiArtifactBlock. That sandbox (no
allow-same-origin) is the actual security boundary; this module's job is
just to size-cap the input and hand back a stable URL.
"""

from __future__ import annotations

import re
from typing import Any

from backend.mcp_servers.code.artifacts import artifact_url, get_artifact_store
from backend.mcp_servers.code.errors import ValidationError

_FILENAME = "index.html"
_CONTENT_TYPE = "text/html"
_HAS_DOCUMENT_SHAPE = re.compile(r"<\s*(!doctype|html)\b", re.IGNORECASE)


def generate_ui(html: str, title: str = "Generated UI", *, settings: Any) -> dict[str, Any]:
    if not isinstance(html, str) or not html.strip():
        raise ValidationError("html must be a non-empty string.")
    if len(html) > settings.max_ui_html_length:
        raise ValidationError(
            f"html is {len(html)} characters, exceeding the {settings.max_ui_html_length}-character limit."
        )

    document = _ensure_document(html, title if isinstance(title, str) and title.strip() else "Generated UI")
    data = document.encode("utf-8")

    if len(data) > settings.max_artifact_bytes:
        raise ValidationError(f"Generated UI is {len(data)} bytes, exceeding the {settings.max_artifact_bytes}-byte limit.")

    store = get_artifact_store()
    artifact_id = store.save(data, _FILENAME)

    return {
        "success": True,
        "filename": _FILENAME,
        "url": artifact_url(settings, artifact_id, _FILENAME),
        "content_type": _CONTENT_TYPE,
        "size_bytes": len(data),
    }


def _ensure_document(html: str, title: str) -> str:
    """Wrap a bare fragment in a minimal HTML document; pass a full document through untouched."""
    if _HAS_DOCUMENT_SHAPE.search(html):
        return html
    escaped_title = title.replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escaped_title}</title>\n</head>\n<body>\n{html}\n</body>\n</html>\n"
    )
