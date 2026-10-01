"""Serves files captured from tool calls — currently: run_python_code output
files (plots, CSVs, ...) and generate_ui's HTML pages, saved by the Code MCP
server via ArtifactStore — plus the chat file-upload endpoint and the
per-user document registry ("my files").
"""
from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from backend.apps.api.auth import AuthUser, get_current_user
from backend.apps.api.session_helpers import get_or_rehydrate_session
from backend.core.storage.artifact_store import ArtifactStore
from backend.core.storage.document_store import DocumentStore

logger = logging.getLogger("agentic_api")

router = APIRouter(prefix="/api/files", tags=["files"])
_store = ArtifactStore()
_document_store = DocumentStore()

_SUPPORTED_UPLOAD_EXTENSIONS = {
    ".pdf", ".csv", ".xlsx", ".xlsm", ".txt", ".log", ".md", ".docx", ".pptx",
}
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25MB

# Attached in the chat inbox (upload_file below), these skip indexing
# entirely — the chat agent analyzes them directly via the code MCP server's
# profile_spreadsheet/run_python_code_on_document (real pandas against the
# actual file), since row-per-chunk vector retrieval can't guarantee "every
# row" or a trustworthy aggregate the way a real dataframe query can. Only
# affects the session-scoped upload endpoint — the standalone Documents page
# (upload_document below) always indexes, regardless of extension, since
# that flow is about durable cross-conversation search rather than one
# chat's worth of analysis.
_SPREADSHEET_EXTENSIONS = {".csv", ".xlsx", ".xlsm"}

# Types the browser should render in place (an <img>, or a generate_ui page
# loaded into an iframe) rather than offer as a "Save As" download. Everything
# else (docx/xlsx/csv/...) keeps the default "attachment" disposition, since
# those are meant to be opened in an external app. This matters specifically
# for the iframe case: unlike an <img> load, a browser treats an iframe's
# src fetch as a navigation, and honors Content-Disposition: attachment by
# downloading the file instead of rendering it — so an inline HTML artifact
# would silently fail to display without this.
_INLINE_CONTENT_TYPES = {"text/html", "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}

# Artifacts are model-generated (a generate_ui page, a plot, an SVG...) and
# served from the API's own origin, so they must never run with that
# origin's privileges. `sandbox` puts the response in an opaque origin even
# when opened directly in a tab (not only inside the frontend's sandboxed
# iframe) — scripts still run, so interactive generate_ui pages keep
# working, but can't read anything belonging to the API origin. nosniff
# stops a browser from re-interpreting e.g. a .txt artifact as HTML.
_ARTIFACT_HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-downloads",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/{artifact_id}/{filename}")
def get_file(artifact_id: str, filename: str) -> FileResponse:
    path = _store.get_path(artifact_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="File not found")
    media_type, _ = mimetypes.guess_type(path.name)
    disposition = "inline" if media_type in _INLINE_CONTENT_TYPES else "attachment"
    return FileResponse(
        path, filename=path.name, content_disposition_type=disposition, headers=_ARTIFACT_HEADERS
    )


# --- Chat file uploads + per-user document registry -------------------------
# Separate router (no /api/files prefix) since these live at /api/sessions/...
# and /api/documents rather than under the artifact-serving path above.
documents_router = APIRouter(tags=["documents"])


async def _store_uploaded_file(request: Request, file: UploadFile, user: AuthUser) -> dict:
    """Shared by every upload path: validates, reads, and persists the raw
    bytes into the document store. Returns {document_id, filename, extension,
    file_bytes} — callers decide from there whether to index or not."""
    filename = file.filename or "upload"
    extension = Path(filename).suffix.lower()
    if extension not in _SUPPORTED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{extension}'. Supported: {', '.join(sorted(_SUPPORTED_UPLOAD_EXTENSIONS))}",
        )

    file_bytes = await file.read()
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB)")

    logger.info("[UPLOAD] user=%s filename=%s size=%d", user.user_id, filename, len(file_bytes))

    # Write straight into the shared document store (settings.document_store.dir,
    # the same directory the indexing MCP server's own DocumentStore reads from —
    # both processes run on the same host) rather than base64-encoding the file
    # into the MCP tool call: fastmcp's streamable-HTTP transport caps a single
    # request body at 4MiB, and base64 alone inflates the payload ~33% on top of
    # that, so anything past a few MB of actual file content used to 413 here.
    # Passing just the document_id keeps the tool call tiny regardless of file size.
    document_id = str(uuid4())
    _document_store.create_document(file_bytes=file_bytes, document_id=document_id, filename=filename)

    return {"document_id": document_id, "filename": filename, "extension": extension, "file_bytes": file_bytes}


async def _upload_and_index(request: Request, file: UploadFile, user: AuthUser) -> dict:
    """Stores + parses + chunks + embeds `file` and registers it in the
    per-user document registry, indexed status. Used by both the standalone
    Documents page and, for non-spreadsheet types, the chat inbox."""
    stored = await _store_uploaded_file(request, file, user)
    document_id, filename, extension = stored["document_id"], stored["filename"], stored["extension"]

    runtime = request.app.state.runtime
    try:
        # Uses the "_v2" (LangChain) indexing pipeline — retrieval on these
        # documents (streaming_agent.py's _with_attached_documents_note) is
        # pointed at the matching "_v2" retrieval tools, since v1/v2 write to
        # separate vector collections and can't read each other's documents.
        result = await runtime.call_tool_isolated(
            "indexing_upload_and_index_document_v2",
            {
                "document_id": document_id,
                "filename": filename,
                "user_id": user.user_id,
            },
        )
    except Exception as exc:  # noqa: BLE001 — surface as a normal HTTP error, not a 500 stack trace
        logger.exception("upload_and_index_document_v2 call failed for %s", filename)
        raise HTTPException(status_code=502, detail=f"Indexing failed: {exc}")

    data = _tool_result_json(result)
    if not data.get("success"):
        raise HTTPException(status_code=422, detail=data.get("error", "Indexing failed"))

    document_id = data["document_id"]
    content_type = data.get("content_type", extension.lstrip("."))
    chunks_indexed = data.get("chunks_indexed", 0)

    registry = request.app.state.document_registry
    if registry is not None:
        await registry.create(
            document_id=document_id,
            user_id=user.user_id,
            original_name=filename,
            stored_name=f"original{extension}",
            file_type=content_type,
            status="indexed",
            chunks_indexed=chunks_indexed,
        )

    return {
        "document_id": document_id,
        "filename": filename,
        "content_type": content_type,
        "chunks_indexed": chunks_indexed,
    }


async def _upload_without_indexing(request: Request, file: UploadFile, user: AuthUser) -> dict:
    """Chat-inbox path for a spreadsheet: store the raw bytes only, skip
    indexing/embedding entirely. The chat agent analyzes it directly with
    the code MCP server's profile_spreadsheet / run_python_code_on_document
    (real pandas against the actual file) instead of row-chunk retrieval —
    see the module-level comment on _SPREADSHEET_EXTENSIONS for why."""
    stored = await _store_uploaded_file(request, file, user)
    document_id, filename, extension = stored["document_id"], stored["filename"], stored["extension"]
    content_type = extension.lstrip(".")

    registry = request.app.state.document_registry
    if registry is not None:
        await registry.create(
            document_id=document_id,
            user_id=user.user_id,
            original_name=filename,
            stored_name=f"original{extension}",
            file_type=content_type,
            status="stored",
            chunks_indexed=0,
        )

    return {
        "document_id": document_id,
        "filename": filename,
        "content_type": content_type,
        "chunks_indexed": 0,
    }


@documents_router.post("/api/sessions/{session_id}/upload")
async def upload_file(
    session_id: str,
    request: Request,
    file: UploadFile,
    user: AuthUser = Depends(get_current_user),
) -> dict:
    session = await get_or_rehydrate_session(request, session_id, user.user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    extension = Path(file.filename or "upload").suffix.lower()
    if extension in _SPREADSHEET_EXTENSIONS:
        result = await _upload_without_indexing(request, file, user)
    else:
        result = await _upload_and_index(request, file, user)
    session.add_attached_document(result["document_id"], result["filename"], result["content_type"])
    return result


@documents_router.post("/api/documents/upload")
async def upload_document(
    request: Request,
    file: UploadFile,
    user: AuthUser = Depends(get_current_user),
) -> dict:
    """Uploads + indexes a document from the standalone Documents page, with
    no chat session involved — the user references it later by document_id
    from any conversation."""
    return await _upload_and_index(request, file, user)


@documents_router.get("/api/documents")
async def list_documents(request: Request, user: AuthUser = Depends(get_current_user)) -> list[dict]:
    registry = request.app.state.document_registry
    if registry is None:
        return []
    return await registry.list_for_user(user.user_id)


@documents_router.get("/api/documents/{document_id}/content")
async def get_document_content(
    document_id: str, request: Request, user: AuthUser = Depends(get_current_user)
) -> FileResponse:
    """Serves a previously uploaded document's original bytes for the
    Documents page's "view" action, after checking the caller owns it."""
    registry = request.app.state.document_registry
    if registry is None or not await registry.is_owner(document_id, user.user_id):
        raise HTTPException(status_code=404, detail="Document not found")

    path = _document_store.get_file_path(document_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Document not found")

    media_type, _ = mimetypes.guess_type(path.name)
    disposition = "inline" if media_type in _INLINE_CONTENT_TYPES or media_type == "application/pdf" else "attachment"
    doc = await registry.get(document_id)
    display_name = (doc or {}).get("original_name") or path.name
    return FileResponse(
        path,
        filename=display_name,
        media_type=media_type,
        content_disposition_type=disposition,
        headers=_ARTIFACT_HEADERS,
    )


@documents_router.get("/api/documents/{document_id}/chunks")
async def get_document_chunks(
    document_id: str, request: Request, user: AuthUser = Depends(get_current_user)
) -> list[dict]:
    """Every chunk of a document's v2 collection, in chunk_index order,
    with its full text/metadata/grounding (chunk_id, chunk_content_type,
    page/sheet/row, h1/h2/h3, ...). Backs the Documents page's chunk
    preview only — chat retrieval always goes through the scored
    retrieve_document_v2 tool instead, never this raw listing."""
    registry = request.app.state.document_registry
    if registry is None or not await registry.is_owner(document_id, user.user_id):
        raise HTTPException(status_code=404, detail="Document not found")

    runtime = request.app.state.runtime
    try:
        result = await runtime.call_tool_isolated(
            "retrieval_get_document_chunks_v2",
            {"document_id": document_id, "user_id": user.user_id},
        )
    except Exception as exc:  # noqa: BLE001 — surface as a normal HTTP error, not a 500 stack trace
        logger.exception("get_document_chunks_v2 call failed for %s", document_id)
        raise HTTPException(status_code=502, detail=f"Failed to load chunks: {exc}")

    data = _tool_result_json(result)
    if not data.get("success"):
        raise HTTPException(status_code=404, detail=data.get("error", "Document not found"))
    return data.get("chunks", [])


@documents_router.delete("/api/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str, request: Request, user: AuthUser = Depends(get_current_user)
) -> None:
    registry = request.app.state.document_registry
    if registry is None:
        raise HTTPException(status_code=503, detail="Document registry is unavailable")
    deleted = await registry.delete(document_id, user.user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")

    # Matches _upload_and_index's indexing_upload_and_index_document_v2 call —
    # a document from this registry was indexed into the v2 collection, so
    # cleanup must target the same one.
    runtime = request.app.state.runtime
    try:
        await runtime.call_tool_isolated("indexing_delete_document_v2", {"document_id": document_id})
    except Exception:  # noqa: BLE001 — the registry entry is already gone; log and move on
        logger.exception("indexing_delete_document_v2 call failed for %s", document_id)


def _tool_result_json(result) -> dict:
    """Mirror streaming_agent._extract_text's unwrapping, then parse the JSON
    text every indexing/retrieval tool returns."""
    content = getattr(result, "content", result)
    if isinstance(content, list):
        text = "\n".join(b.text for b in content if hasattr(b, "text"))
    else:
        text = str(content)
    return json.loads(text)
