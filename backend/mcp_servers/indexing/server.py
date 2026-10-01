"""Indexing MCP server: turns uploaded files into searchable chunks.

Two tools, both called by the API (routers/files.py), not by the chat model:
- upload_and_index_document_v2: parse -> chunk -> embed -> store in Postgres
- delete_document_v2: remove a document's stored file and its index

The earlier llama_index/Chroma ("v1") tools and the tools that read a
server-local `file_path` were removed — the first because everything is v2
now, the second because "read any file on this server" is not something an
MCP server reachable by an LLM should offer.
"""
import sys
import os
from pathlib import Path

# Add project root to Python path to support direct script execution
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from fastmcp import FastMCP

from backend.core.mcp_http import run_mcp_http
from backend.config import settings
from backend.core.db import apply_migrations
from backend.core.storage.document_store import DocumentStore
from backend.mcp_servers.indexing.parsers.detect import detect_document_type_from_filename
from backend.mcp_servers.indexing.langchain_pipeline import LangchainIndexPipelineV2
import logging
from uuid import uuid4
import base64

# Ensure logs directory exists
log_dir = Path(__file__).parent.parent.parent.parent / "logs"
log_dir.mkdir(exist_ok=True)

logger = logging.getLogger(__name__)
handlers = [
    logging.FileHandler(log_dir / "indexing_server.log", encoding="utf-8"),
    logging.StreamHandler()
]
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
for handler in handlers:
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

store = DocumentStore()
indexer_v2 = LangchainIndexPipelineV2()
mcp = FastMCP("indexing")


@mcp.tool
def upload_and_index_document_v2(
    file_b64: str | None = None,
    filename: str = "",
    user_id: str = "",
    document_id: str | None = None,
) -> dict:
    """
    Parse, chunk, embed and index a document (pdf, csv, xlsx/xlsm/xls/ods,
    txt, md, log, docx, pptx) into Postgres for retrieve_document_v2 and the
    other retrieval `_v2` tools. Called by the API's upload endpoints, never
    by the chat model (see API_MODEL_TOOLS).

    Two ways to supply the file — pass exactly one:
    - file_b64: raw bytes, base64-encoded, for a small file. FastMCP's
      streamable-HTTP transport caps a single request body at 4MiB (see
      mcp.server.streamable_http_manager.DEFAULT_MAX_REQUEST_BODY_SIZE), and
      base64 alone inflates the payload ~33% on top of that, so this mode
      413s well under 4MiB of actual file content.
    - document_id: an existing document already written to the shared
      document store (backend.core.storage.document_store.DocumentStore,
      at settings.document_store.dir) by the caller — e.g. the API layer's
      routers/files.py, which writes the upload straight to that shared
      directory itself and passes only the (tiny) document_id here, so a
      large file's bytes never travel over this MCP call at all. The
      document_id must already have a file at
      DocumentStore.get_file_path(document_id) or this returns an error;
      the document is then indexed under that same document_id (a fresh one
      is not minted, unlike the file_b64 path).

    `user_id` is the document's owner — only that user can retrieve it.
    Always supplied by the calling application, never asked of an end user;
    required.

    Args:
        file_b64: The file's raw bytes, base64-encoded (small-file mode).
        filename: Original filename (used to detect type and for display)
        user_id: Owner of this document
        document_id: UUID of a file already stored via DocumentStore
            (large-file mode — mutually exclusive with file_b64)

    Returns:
        A dictionary with document_id, content_type, chunks_indexed, and
        pipeline="langchain_v2".
    """
    if not filename:
        return {"success": False, "error": "filename is required"}
    if not user_id:
        return {"success": False, "error": "user_id is required"}

    file_path = None
    file_bytes = None
    if document_id:
        logger.info(f"[UPLOAD_AND_INDEX_V2] user={user_id} filename={filename} using pre-stored document_id={document_id}")
        file_path = store.get_file_path(document_id)
        if file_path is None:
            logger.warning(f"[UPLOAD_AND_INDEX_V2] user={user_id} no stored file for document_id={document_id}")
            return {"success": False, "document_id": document_id, "error": f"No stored file found for document_id={document_id}"}
        fallback_bytes = file_path.read_bytes()[:4096]
    elif file_b64:
        document_id = str(uuid4())
        logger.info(f"[UPLOAD_AND_INDEX_V2] user={user_id} filename={filename} UUID: {document_id}")
        try:
            file_bytes = base64.b64decode(file_b64)
        except Exception as e:
            logger.warning(f"[UPLOAD_AND_INDEX_V2] user={user_id} invalid base64 for {filename}: {e}")
            return {"success": False, "error": f"Invalid base64 file content: {e}"}
        fallback_bytes = file_bytes[:4096]
    else:
        return {"success": False, "error": "Either file_b64 or document_id (for an already-stored file) must be provided"}

    try:
        content_type = detect_document_type_from_filename(filename, fallback_bytes=fallback_bytes)
    except ValueError as e:
        logger.warning(f"[UPLOAD_AND_INDEX_V2] user={user_id} rejected {filename}: {e}")
        return {"success": False, "document_id": document_id, "error": str(e)}

    try:
        if file_path is None:  # file_b64 mode — not yet written to disk
            assert file_bytes is not None
            file_path = store.create_document(file_bytes=file_bytes, document_id=document_id, filename=filename)
            logger.info(f"[UPLOAD_AND_INDEX_V2] user={user_id} stored {document_id} as {content_type} ({len(file_bytes)} bytes)")

        index_result = indexer_v2.index_document(
            document_id=document_id,
            file_path=file_path,
            content_type=content_type,
            document_name=filename,
            user_id=user_id,
        )

        if not index_result.get("success"):
            logger.error(f"[UPLOAD_AND_INDEX_V2] user={user_id} indexing failed for {document_id}: {index_result.get('error')}")
            return {"success": False, "document_id": document_id, "error": index_result.get("error", "Indexing failed")}

        logger.info(
            f"[UPLOAD_AND_INDEX_V2] user={user_id} indexed {document_id}: "
            f"{index_result.get('chunks_indexed', 0)} chunks"
        )

        return {
            "success": True,
            "document_id": document_id,
            "filename": filename,
            "content_type": content_type,
            "chunks_indexed": index_result.get("chunks_indexed", 0),
            "pipeline": "langchain_v2",
        }
    except Exception as e:
        logger.error(f"[UPLOAD_AND_INDEX_V2] user={user_id} failed for {document_id}: {str(e)}", exc_info=True)
        return {"success": False, "document_id": document_id, "error": str(e)}


@mcp.tool
def delete_document_v2(document_id: str) -> dict:
    """
    Permanently delete a document indexed via upload_and_index_document_v2:
    removes its stored file and its chunks/embeddings. Called by the API's
    delete endpoint (after its own ownership check), never by the chat model.

    Args:
        document_id: UUID of the document to delete

    Returns:
        A dictionary with success and document_id
    """
    logger.info(f"[DELETE_DOCUMENT_V2] Deleting {document_id}")
    vector_result = indexer_v2.delete_document(document_id)
    try:
        store.delete(document_id)
    except Exception as e:
        logger.error(f"[DELETE_DOCUMENT_V2] Failed to delete stored file for {document_id}: {str(e)}")
        return {"success": False, "document_id": document_id, "error": str(e)}

    return {"success": True, "document_id": document_id, "vector_deleted": vector_result.get("success", False)}


if __name__ == "__main__":
    # Make sure the `rag` schema exists before the first upload (the API runs
    # the same idempotent migrations; whichever process starts first wins).
    apply_migrations(settings.database.url)

    # MCP_TRANSPORT=stdio lets this server be launched as a subprocess
    # (e.g. from servers.docker.json) instead of a standalone HTTP service.
    if os.environ.get("MCP_TRANSPORT", "http") == "stdio":
        mcp.run(transport="stdio")
    else:
        run_mcp_http(mcp, settings.indexing_server.host, settings.indexing_server.port)
