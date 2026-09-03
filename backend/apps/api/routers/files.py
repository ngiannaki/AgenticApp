"""Serves files captured from tool calls — currently: run_python_code output
files (plots, CSVs, ...) saved by the Code MCP server via ArtifactStore.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.core.storage.artifact_store import ArtifactStore

router = APIRouter(prefix="/api/files", tags=["files"])
_store = ArtifactStore()


@router.get("/{artifact_id}/{filename}")
def get_file(artifact_id: str, filename: str) -> FileResponse:
    path = _store.get_path(artifact_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=path.name)
