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
from backend.mcp_servers.indexing.parsers.pdf import parse_pdf
from backend.infrastructure.vector_store import VectorStore
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
mcp = FastMCP("indexing")

@mcp.tool
def upload_document(pdf_base64: str = None, file_path: str = None) -> dict: # type: ignore
    """
    Upload a PDF document to the server.

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
            pdf_bytes=pdf_bytes,
            document_id=document_id,
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
            pdf_bytes=pdf_bytes,
            document_id=document_id,
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

@mcp.tool
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