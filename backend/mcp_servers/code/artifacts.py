"""Shared ArtifactStore access for the Code MCP server's tools.

Both run_python_code (captured output files) and generate_file (directly
produced documents) persist through the same store and URL scheme, so this
lives in one place rather than being duplicated per tool.
"""

from __future__ import annotations

from typing import Any, Optional

from backend.core.storage.artifact_store import ArtifactStore

_artifact_store: Optional[ArtifactStore] = None


def get_artifact_store() -> ArtifactStore:
    global _artifact_store
    if _artifact_store is None:
        _artifact_store = ArtifactStore()
    return _artifact_store


def artifact_url(settings: Any, artifact_id: str, filename: str) -> str:
    return f"{settings.artifact_public_base_url}/api/files/{artifact_id}/{filename}"
