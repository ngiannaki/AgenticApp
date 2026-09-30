import sys
import os
from pathlib import Path

# Add project root to Python path to support direct script execution
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from fastmcp import FastMCP
from backend.config import settings
from backend.core.storage.document_store import DocumentStore
from backend.mcp_servers.indexing.parsers.csv_reader import parse_csv
from backend.mcp_servers.indexing.parsers.detect import detect_document_type_from_filename
from backend.mcp_servers.indexing.parsers.docx_reader import parse_docx
from backend.mcp_servers.indexing.parsers.pdf import parse_pdf
from backend.mcp_servers.indexing.parsers.pptx_reader import parse_pptx
from backend.mcp_servers.indexing.parsers.reader import read_document as _read_document
from backend.mcp_servers.indexing.parsers.txt_reader import parse_txt
from backend.mcp_servers.indexing.parsers.xlsx_reader import parse_xlsx
from backend.infrastructure.vector_store import VectorStore
from backend.mcp_servers.indexing.langchain_pipeline import LangchainIndexPipelineV2
import logging
from uuid import uuid4
import base64
import json

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
vector_store = VectorStore()
indexer_v2 = LangchainIndexPipelineV2()
mcp = FastMCP("indexing")

@mcp.tool
def upload_document(pdf_base64: str = None, file_path: str = None) -> dict: # type: ignore
    """
    Upload a PDF document to the server so it can be parsed and indexed
    (see parse_and_index_enhanced) into the vector store for later search.

    PDF only — takes exactly these two optional args, nothing else (no
    file_name/filename param exists; the server generates a document_id and
    that's what identifies the document afterward). For CSV, XLSX, TXT/MD,
    or a PDF you just want to read (not index for search), use
    read_document(file_path) instead — it reads the file directly and
    returns its content in one call, no document_id/store involved.

    Can accept either:
    - pdf_base64: The PDF file encoded as base64 string (for small files)
    - file_path: Local file path to the PDF (recommended for large files)

    Args:
        pdf_base64: The PDF file encoded as base64 string (optional)
        file_path: Local file path to PDF file (optional)

    Returns:
        A dictionary with the document_id
    """
    if not pdf_base64 and not file_path:
        return {
            "success": False,
            "error": "Either pdf_base64 or file_path must be provided"
        }
    
    document_id = str(uuid4())
    logger.info(f"[UPLOAD] Generated UUID: {document_id}")
    
    try:
        # Handle file path input
        if file_path:
            logger.info(f"[UPLOAD] Reading PDF from file path: {file_path}")
            pdf_path = Path(file_path)
            
            if not pdf_path.exists():
                return {
                    "success": False,
                    "error": f"File not found: {file_path}"
                }
            
            pdf_bytes = pdf_path.read_bytes()
        else:
            # Handle base64 input
            logger.info(f"[UPLOAD] Decoding base64 PDF")
            pdf_bytes = base64.b64decode(pdf_base64)
        
        store.create_document(
            file_bytes=pdf_bytes,
            document_id=document_id,
            filename="document.pdf",
        )
        logger.info(f"[UPLOAD] Stored PDF for UUID: {document_id} ({len(pdf_bytes)} bytes)")
        
        return {
            "success": True,
            "document_id": document_id,
            "file_size": len(pdf_bytes),
            "message": f"Document uploaded successfully with ID: {document_id}"
        }
    except Exception as e:
        logger.error(f"[UPLOAD] Failed for UUID {document_id}: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }

@mcp.tool
def upload_document_bytes(pdf_bytes: bytes) -> dict:
    """
    Upload a PDF document to the server by sending its raw bytes directly.

    PDF only — takes exactly this one arg (no file_name/filename param; the
    server generates a document_id). For CSV, XLSX, or TXT/MD, or a PDF you
    just want to read rather than index for search, use read_document
    instead (note: read_document takes a server-local file_path, not bytes).

    Use this instead of upload_document's file_path option when the MCP
    client and server are not on the same machine (e.g. connecting through
    nginx/Cloudflare), since file_path is resolved on the server's local
    filesystem and won't see files that only exist on the client.

    FastMCP transmits `bytes` parameters as base64 over the wire and decodes
    them automatically, so callers just pass the raw file bytes.

    Args:
        pdf_bytes: Raw bytes of the PDF file

    Returns:
        A dictionary with the document_id
    """
    document_id = str(uuid4())
    logger.info(f"[UPLOAD_BYTES] Generated UUID: {document_id}")

    try:
        store.create_document(
            file_bytes=pdf_bytes,
            document_id=document_id,
            filename="document.pdf",
        )
        logger.info(f"[UPLOAD_BYTES] Stored PDF for UUID: {document_id} ({len(pdf_bytes)} bytes)")

        return {
            "success": True,
            "document_id": document_id,
            "file_size": len(pdf_bytes),
            "message": f"Document uploaded successfully with ID: {document_id}"
        }
    except Exception as e:
        logger.error(f"[UPLOAD_BYTES] Failed for UUID {document_id}: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }


def _parse_for_indexing(file_path: Path, content_type: str) -> dict:
    """Dispatch to the right parser and normalize its result into the shape
    index_document_enhanced expects: 'pages'+'full_text'+'markdown' for
    pdf/pptx/docx/txt, or 'sheets' for csv/xlsx."""
    if content_type == "pdf":
        return parse_pdf(str(file_path))
    if content_type == "pptx":
        return parse_pptx(file_path)
    if content_type == "docx":
        return parse_docx(file_path)
    if content_type == "txt":
        return parse_txt(file_path)
    if content_type == "csv":
        result = parse_csv(file_path)
        return {"sheets": [{"name": file_path.stem, "headers": result["headers"], "rows": result["rows"]}]}
    if content_type == "xlsx":
        result = parse_xlsx(file_path)
        return {"sheets": [{"name": s["name"], "headers": s["headers"], "rows": s["rows"]} for s in result["sheets"]]}
    raise ValueError(f"Unsupported content type: {content_type}")


#@mcp.tool
def upload_and_index_document(file_b64: str, filename: str, user_id: str) -> dict:
    """
    Upload and index a document of any supported type in one step:
    pdf, csv, xlsx/xlsm, txt, md, log, docx, or pptx.

    Detects the type from `filename`'s extension, stores the raw bytes,
    parses it with the matching reader, and indexes it into the vector store
    using type-appropriate chunking (row-group chunks for csv/xlsx,
    heading/page-aware hierarchical chunks for everything else) so it becomes
    retrievable via retrieve_document(document_id, query).

    `user_id` scopes ownership — every chunk's metadata is stamped with it,
    and retrieve_document/get_document_info refuse to return results for a
    document belonging to a different user_id. This argument is always
    supplied by the calling application (never by an end user's free-text
    request) — do not ask the user for a user_id.

    Args:
        file_b64: The file's raw bytes, base64-encoded. Deliberately a plain
            `str` decoded manually below rather than a `bytes`-typed
            parameter — a `bytes` parameter's JSON-over-the-wire coercion is
            ambiguous (pydantic's default treats an incoming JSON string as
            literal UTF-8 text, not base64, so arbitrary binary — e.g. a PDF
            — would round-trip corrupted or fail outright on non-UTF-8 bytes).
        filename: Original filename (used to detect type and for display)
        user_id: Owner of this document

    Returns:
        A dictionary with document_id, content_type, and chunks_indexed
    """
    document_id = str(uuid4())
    logger.info(f"[UPLOAD_AND_INDEX] user={user_id} filename={filename} UUID: {document_id}")

    try:
        file_bytes = base64.b64decode(file_b64)
    except Exception as e:
        logger.warning(f"[UPLOAD_AND_INDEX] user={user_id} invalid base64 for {filename}: {e}")
        return {"success": False, "error": f"Invalid base64 file content: {e}"}

    try:
        content_type = detect_document_type_from_filename(filename, fallback_bytes=file_bytes[:4096])
    except ValueError as e:
        logger.warning(f"[UPLOAD_AND_INDEX] user={user_id} rejected {filename}: {e}")
        return {"success": False, "error": str(e)}

    try:
        file_path = store.create_document(file_bytes=file_bytes, document_id=document_id, filename=filename)
        logger.info(f"[UPLOAD_AND_INDEX] user={user_id} stored {document_id} as {content_type} ({len(file_bytes)} bytes)")

        parsed_content = _parse_for_indexing(file_path, content_type)

        index_result = vector_store.index_document_enhanced(
            document_id=document_id,
            parsed_content=parsed_content,
            document_name=filename,
            use_hierarchical=True,
            extract_metadata=True,
            content_type=content_type,
            extra_metadata={"user_id": user_id},
        )

        if not index_result.get("success"):
            logger.error(f"[UPLOAD_AND_INDEX] user={user_id} indexing failed for {document_id}: {index_result.get('error')}")
            return {"success": False, "document_id": document_id, "error": index_result.get("error", "Indexing failed")}

        logger.info(
            f"[UPLOAD_AND_INDEX] user={user_id} indexed {document_id}: "
            f"{index_result.get('chunks_indexed', 0)} chunks"
        )

        return {
            "success": True,
            "document_id": document_id,
            "filename": filename,
            "content_type": content_type,
            "chunks_indexed": index_result.get("chunks_indexed", 0),
        }
    except Exception as e:
        logger.error(f"[UPLOAD_AND_INDEX] user={user_id} failed for {document_id}: {str(e)}", exc_info=True)
        return {"success": False, "document_id": document_id, "error": str(e)}


#@mcp.tool
def delete_document(document_id: str) -> dict:
    """
    Permanently delete a document: removes its stored file and its vector
    collection from ChromaDB. Does not touch the per-user document registry
    (the API layer owns that) — call this alongside removing the registry
    entry, not instead of it.

    Args:
        document_id: UUID of the document to delete

    Returns:
        A dictionary with success and document_id
    """
    logger.info(f"[DELETE_DOCUMENT] Deleting {document_id}")
    vector_result = vector_store.delete_collection(document_id)
    try:
        store.delete(document_id)
    except Exception as e:
        logger.error(f"[DELETE_DOCUMENT] Failed to delete stored file for {document_id}: {str(e)}")
        return {"success": False, "document_id": document_id, "error": str(e)}

    return {"success": True, "document_id": document_id, "vector_deleted": vector_result.get("success", False)}


@mcp.tool
def upload_and_index_document_v2(
    file_b64: str | None = None,
    filename: str = "",
    user_id: str = "",
    document_id: str | None = None,
) -> dict:
    """
    LangChain-native equivalent of upload_and_index_document: same supported
    types (pdf, csv, xlsx/xlsm/xls/ods, txt, md, log, docx, pptx), but parses
    with LangChain document loaders and indexes into a separate v2 vector
    collection using LangChain's text splitters + Chroma integration instead
    of llama_index.

    Use this (paired with retrieve_document_v2 in the retrieval server) when
    you want the state-of-the-art hybrid retrieval + reranking pipeline;
    use upload_and_index_document/retrieve_document for the original
    pipeline. The two pipelines are fully independent — a document indexed
    with one is invisible to the other (different Chroma collections), so
    index with whichever tool you intend to later retrieve with.

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

    `user_id` scopes ownership exactly like upload_and_index_document — it
    is always supplied by the calling application, never asked of an end
    user.

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
    removes its stored file and its v2 vector collection from ChromaDB.

    This does not touch a v1 collection for the same document_id (if any) —
    call delete_document too if the document was also indexed with v1.

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


#@mcp.tool
def read_document(file_path: str, format: str = "markdown") -> dict:
    """Read a PDF, CSV, XLSX, or TXT/MD/LOG file and return its content.

    Detects the file's type from its extension (falling back to magic bytes
    for a mislabeled extension, and to a text-decode check for an unknown
    one) and parses it with the matching reader — Docling (falling back to
    pymupdf) for PDF, the stdlib csv module (with delimiter sniffing) for
    CSV, openpyxl for XLSX, plain UTF-8 for TXT/MD/LOG. Use this instead of
    upload_document + parse_document when you just need a file's content
    read into the conversation (not indexed into the vector store), or for
    any format other than PDF.

    `format` controls how `content` is rendered:
    - "markdown" (default): tables render as real `| markdown | tables |`;
      PDFs additionally get heading structure from Docling's layout
      analysis when Docling is available, falling back to plain text
      wrapped with `## Page N` headers when it isn't.
    - "text": flat plain text, no markdown syntax.
    Prefer markdown when you'll show the content to a user or another LLM
    step (it preserves table/heading structure); use text if you need the
    raw content with no formatting markup.

    `file_path` is resolved on the server's local filesystem — same
    constraint as upload_document's file_path option (see
    upload_document_bytes if the file only exists on the client).

    Returns `{success, type, format, filename, content, ...}` on success,
    where `content` is the rendering in the requested format and `...` is
    type-specific: PDF adds `page_count`/`pages`; CSV adds
    `headers`/`rows`/`row_count`/`column_count`; XLSX adds
    `sheets`/`sheet_count` (each sheet has its own `headers`/`rows`); TXT
    adds `line_count`/`char_count`. On failure returns `{success: False,
    error}`.
    """
    logger.info(f"[READ_DOCUMENT] Reading file: {file_path} (format={format})")

    try:
        result = _read_document(file_path, format=format)
        result["success"] = True
        logger.info(
            f"[READ_DOCUMENT] Parsed {file_path} as type={result.get('type')}"
        )
        return result
    except Exception as e:
        logger.error(f"[READ_DOCUMENT] Failed for {file_path}: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }


def parse_document(document_id: str) -> dict:
    """
    Parse a stored PDF document.

    The caller only needs the document_id.
    """
    logger.info(f"[PARSE] Parsing document with UUID: {document_id}")

    if not store.exists(document_id):
        logger.error(f"[PARSE] Document not found for UUID: {document_id}")
        raise FileNotFoundError(
            f"Document not found: {document_id}"
        )

    pdf_path = store.get_pdf_path(
        document_id
    )
    
    logger.info(f"[PARSE] PDF found at: {pdf_path}")
    result = parse_pdf(pdf_path.__str__())
    logger.info(f"[PARSE] Successfully parsed {result.get('page_count', 0)} pages for UUID: {document_id}")
    
    return result

def index_document(document_id: str, parsed_content: str) -> dict:
    """
    Index a parsed PDF document into the vector store using ChromaDB and LlamaIndex.
    
    Chunks are created from pages and stored with the document_id for later retrieval.
    
    Args:
        document_id: UUID of the document
        parsed_content: JSON string of parsed content from parse_document tool
    
    Returns:
        Dictionary with indexing status
    """
    logger.info(f"[INDEX] Starting indexing for UUID: {document_id}")
    
    try:
        # Parse the JSON string if it comes as a string
        if isinstance(parsed_content, str):
            content = json.loads(parsed_content)
        else:
            content = parsed_content
        
        # Create chunks from pages
        chunks = []
        
        if "pages" in content:
            # Use individual pages as chunks
            for page in content["pages"]:
                chunk = {
                    "text": page.get("text", ""),
                    "metadata": {
                        "page_number": page.get("page_number", 0),
                        "source": "pdf_page"
                    }
                }
                chunks.append(chunk)
        else:
            # Fallback: use full_text as single chunk
            chunk = {
                "text": content.get("full_text", ""),
                "metadata": {
                    "source": "full_text"
                }
            }
            chunks.append(chunk)
        
        if not chunks or not chunks[0]["text"]:
            logger.warning(f"[INDEX] No content to index for UUID: {document_id}")
            return {
                "success": False,
                "document_id": document_id,
                "error": "No content to index"
            }
        
        logger.info(f"[INDEX] Created {len(chunks)} chunks for UUID: {document_id}")
        
        # Index using vector store
        result = vector_store.index_document(document_id, chunks)
        
        if result.get("success"):
            logger.info(f"[INDEX] Successfully indexed UUID: {document_id}")
            logger.info(f"[INDEX] Collection: {result.get('collection_name')}")
            logger.info(f"[INDEX] Persist path: {result.get('persist_path')}")
        else:
            logger.error(f"[INDEX] Failed to index UUID {document_id}: {result.get('error', 'Unknown error')}")
        
        return result
    
    except Exception as e:
        logger.error(f"[INDEX] Failed for UUID {document_id}: {str(e)}", exc_info=True)
        return {
            "success": False,
            "document_id": document_id,
            "error": str(e)
        }

#@mcp.tool
def parse_and_index(document_id: str, use_hierarchical: bool = True) -> dict:
    """
    Parse and index a document in one step.

    Uses hierarchical chunking by default for better semantic structure.

    Args:
        document_id: UUID of the document to parse and index
        use_hierarchical: Use hierarchical chunking (default: True for production)

    Returns:
        Dictionary with indexing status
    """
    logger.info(f"[PARSE+INDEX] Starting parse and index for UUID: {document_id}")
    logger.info(f"[PARSE+INDEX] Hierarchical chunking: {use_hierarchical}")

    try:
        # Parse the document
        parse_result = parse_document(document_id)

        if not parse_result.get("pages") and not parse_result.get("full_text"):
            logger.warning(f"[PARSE+INDEX] No content to index for UUID: {document_id}")
            return {
                "success": False,
                "document_id": document_id,
                "error": "No content to index after parsing"
            }

        # Use enhanced indexing with hierarchical chunking
        index_result = vector_store.index_document_enhanced(
            document_id=document_id,
            parsed_content=parse_result,
            document_name="Untitled",
            use_hierarchical=use_hierarchical,
            extract_metadata=True
        )

        return index_result

    except Exception as e:
        logger.error(f"[PARSE+INDEX] Failed for UUID {document_id}: {str(e)}", exc_info=True)
        return {
            "success": False,
            "document_id": document_id,
            "error": str(e)
        }

#@mcp.tool
def parse_and_index_enhanced(
    document_id: str,
    document_name: str = "Untitled",
    use_hierarchical: bool = True
) -> dict:
    """
    Parse and index a document with enhanced metadata extraction.
    
    Features:
    - Hierarchical chunking (semantic relationships between chunks)
    - Document summary extraction via LLM
    - Section/topic extraction
    - Document statistics (word count, structure)
    - Enhanced chunk metadata
    
    Args:
        document_id: UUID of the document to parse and index
        document_name: Original document file name (for metadata)
        use_hierarchical: Use hierarchical chunking for better semantic structure
    
    Returns:
        Dictionary with indexing status and enhanced metadata
    """
    logger.info(f"[ENHANCED PARSE+INDEX] Starting enhanced parse and index for UUID: {document_id}")
    logger.info(f"[ENHANCED PARSE+INDEX] Document name: {document_name}, Hierarchical: {use_hierarchical}")
    
    try:
        # Parse the document
        parse_result = parse_document(document_id)
        
        if not parse_result.get("pages") and not parse_result.get("full_text"):
            logger.warning(f"[ENHANCED PARSE+INDEX] No content to index for UUID: {document_id}")
            return {
                "success": False,
                "document_id": document_id,
                "error": "No content to index after parsing"
            }
        
        # Index with enhanced metadata
        index_result = vector_store.index_document_enhanced(
            document_id=document_id,
            parsed_content=parse_result,
            document_name=document_name,
            use_hierarchical=use_hierarchical,
            extract_metadata=True
        )
        
        return index_result
    
    except Exception as e:
        logger.error(f"[ENHANCED PARSE+INDEX] Failed for UUID {document_id}: {str(e)}", exc_info=True)
        return {
            "success": False,
            "document_id": document_id,
            "error": str(e)
        }

if __name__ == "__main__":
    # MCP_TRANSPORT=stdio lets this server be launched as a subprocess
    # (e.g. from servers.docker.json) instead of a standalone HTTP service —
    # see backend/apps/client/servers.docker.json and the root Dockerfile.
    # The large-request-size fallback below only applies to HTTP transport.
    if os.environ.get("MCP_TRANSPORT", "http") == "stdio":
        mcp.run(transport="stdio")
        raise SystemExit(0)

    # Configure server with larger request size limit
    import uvicorn

    try:
        # Try using FastMCP with HTTP transport and custom config
        mcp.run(
            transport="http",
            host=settings.indexing_server.host,
            port=settings.indexing_server.port,
        )
    except TypeError:
        # Fallback: Run with uvicorn directly
        # This allows us to set larger limits
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request
        from starlette.responses import Response
        
        # Increase max body size for POST requests
        class LargeBodyMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                if request.method == "POST":
                    # Allow up to 100MB
                    if hasattr(request, '_receive'):
                        request._receive = self._wrapped_receive(request._receive)
                return await call_next(request)
            
            def _wrapped_receive(self, receive):
                async def wrapped_receive():
                    message = await receive()
                    return message
                return wrapped_receive
        
        app = mcp.app # type: ignore
        app.add_middleware(LargeBodyMiddleware)
        
        uvicorn.run(
            app,
            host=settings.indexing_server.host,
            port=settings.indexing_server.port,
            log_level="info",
        )