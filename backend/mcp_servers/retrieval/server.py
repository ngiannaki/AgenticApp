import sys
from pathlib import Path

# Add project root to Python path to support direct script execution
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from fastmcp import FastMCP
import logging
from backend.mcp_servers.retrieval.retriever import DocumentRetriever

# Ensure logs directory exists
log_dir = Path(__file__).parent.parent.parent.parent / "logs"
log_dir.mkdir(exist_ok=True)

logger = logging.getLogger(__name__)
handlers = [
    logging.FileHandler(log_dir / "retrieval_server.log", encoding="utf-8"),
    logging.StreamHandler()
]
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
for handler in handlers:
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

mcp = FastMCP("retrieval")

# Initialize retriever
retriever = DocumentRetriever()


#@mcp.tool
def query_document(document_id: str, query: str) -> dict:
    """
    Query a document using RAG with Ollama.
    
    Takes a document_id and question, retrieves relevant chunks from the vector store,
    and uses Ollama to generate a contextual answer.
    
    Args:
        document_id: UUID of the document to query
        query: Question to ask about the document
    
    Returns:
        Dictionary with answer and source chunks used
    """
    logger.info(f"[RETRIEVAL] query_document called")
    logger.info(f"[RETRIEVAL] document_id: {document_id}")
    logger.info(f"[RETRIEVAL] query: {query}")
    
    result = retriever.query(document_id, query)
    
    if result.get("success"):
        logger.info(f"[RETRIEVAL] Query successful, {result.get('chunks_count', 0)} chunks used")
    else:
        logger.error(f"[RETRIEVAL] Query failed: {result.get('error')}")
    
    return result


#@mcp.tool
def query_document_with_context(document_id: str, query: str, top_k: int = 5) -> dict:
    """
    Query a document with configurable retrieval context.
    
    Args:
        document_id: UUID of the document to query
        query: Question to ask about the document
        top_k: Number of top chunks to retrieve (default: 5)
    
    Returns:
        Dictionary with answer and source chunks used
    """
    logger.info(f"[RETRIEVAL] query_document_with_context called")
    logger.info(f"[RETRIEVAL] document_id: {document_id}, top_k: {top_k}")
    
    result = retriever.query(document_id, query, top_k=top_k)
    
    if result.get("success"):
        logger.info(f"[RETRIEVAL] ✓ Query successful with top_k={top_k}")
    else:
        logger.error(f"[RETRIEVAL] ✗ Query failed: {result.get('error')}")
    
    return result

@mcp.tool
def retrieve_document(
    document_id: str,
    query: str,
    top_k: int = 5,
) -> dict:
    """
    Retrieve relevant chunks from a document.

    This tool performs vector retrieval only.
    It does not generate an answer.
    """
    logger.info(f"[RETRIEVAL] retrieve_document called")
    logger.info(f"[RETRIEVAL] document_id: {document_id}, query: {query}, top_k: {top_k}")

    if not query or not query.strip():
        logger.error("[RETRIEVAL] Query is empty or None")
        return {
            "success": False,
            "document_id": document_id,
            "error": "Query parameter is required and cannot be empty"
        }

    result = retriever.retrieve(
        document_id=document_id,
        query=query,
        top_k=top_k,
    )

    if result.get("success"):
        logger.info(f"[RETRIEVAL] Retrieved {result.get('chunks_count', 0)} chunks")
    else:
        logger.error(f"[RETRIEVAL] Retrieval failed: {result.get('error')}")

    return result

@mcp.tool
def get_document_info(document_id: str) -> dict:
    """
    Get enhanced metadata and information about a document.
    
    Retrieves:
    - Document name
    - Total chunks
    - Sections (if extracted)
    - Hierarchy distribution (chunk levels)
    - Total pages (if multi-page)
    - Collection metadata
    
    Args:
        document_id: UUID of the document
    
    Returns:
        Dictionary with document metadata and statistics
    """
    logger.info(f"[RETRIEVAL] get_document_info called for document_id: {document_id}")
    
    result = retriever.get_document_metadata(document_id)
    
    if result.get("success"):
        logger.info(f"[RETRIEVAL] Retrieved metadata: {result.get('total_chunks', 0)} chunks")
    else:
        logger.error(f"[RETRIEVAL] Failed to get metadata: {result.get('error')}")
    
    return result


#@mcp.tool
def summarize_document(document_id: str, query: str) -> dict:
    """
    Summarize a document using the provided query as context.
    
    Args:
        document_id: UUID of the document to summarize
        query: Context or question to guide the summary
    Returns:
        Dictionary with summary results
    """
    logger.info(f"[RETRIEVAL] summarize_document called for document_id: {document_id}")
    
    result = retriever.summarize_document(document_id, query)
    
    if result.get("success"):
        logger.info(f"[RETRIEVAL] Summarization successful")
    else:
        logger.error(f"[RETRIEVAL] Summarization failed: {result.get('error')}")
    

    return result
if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=8001,
    )