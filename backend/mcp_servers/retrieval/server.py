import os
import sys
from pathlib import Path

# Add project root to Python path to support direct script execution
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from fastmcp import FastMCP
import logging
from backend.config import settings
from backend.mcp_servers.retrieval.retriever import DocumentRetriever
from backend.mcp_servers.retrieval.langchain_pipeline import LangchainRetrievalPipelineV2
from backend.mcp_servers.retrieval.web_search import fetch_page as _fetch_page
from backend.mcp_servers.retrieval.web_search import web_search as _web_search

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
retriever_v2 = LangchainRetrievalPipelineV2()


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

#@mcp.tool
def retrieve_document(
    document_id: str,
    query: str,
    top_k: int = 5,
    user_id: str = "",
) -> dict:
    """
    Retrieve relevant chunks from a document.

    This tool performs vector retrieval only.
    It does not generate an answer.

    `user_id` is always injected by the calling application (never asked of
    the end user) and is checked against the document's owner — a mismatch
    returns the same "not found" error as a nonexistent document_id, so a
    caller can't distinguish "wrong owner" from "doesn't exist".
    """
    logger.info(f"[RETRIEVAL] retrieve_document called")
    logger.info(f"[RETRIEVAL] user_id: {user_id}, document_id: {document_id}, query: {query}, top_k: {top_k}")

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
        user_id=user_id or None,
    )

    if result.get("success"):
        logger.info(f"[RETRIEVAL] Retrieved {result.get('chunks_count', 0)} chunks")
    else:
        logger.error(f"[RETRIEVAL] Retrieval failed: {result.get('error')}")

    return result

#@mcp.tool
def get_document_info(document_id: str, user_id: str = "") -> dict:
    """
    Get enhanced metadata and information about a document.

    Retrieves:
    - Document name
    - Total chunks
    - Sections (if extracted)
    - Hierarchy distribution (chunk levels)
    - Total pages (if multi-page)
    - Collection metadata

    `user_id` is always injected by the calling application and checked
    against the document's owner (see retrieve_document's docstring).

    Args:
        document_id: UUID of the document

    Returns:
        Dictionary with document metadata and statistics
    """
    logger.info(f"[RETRIEVAL] get_document_info called for document_id: {document_id}, user_id: {user_id}")

    result = retriever.get_document_metadata(document_id, user_id=user_id or None)
    
    if result.get("success"):
        logger.info(f"[RETRIEVAL] Retrieved metadata: {result.get('total_chunks', 0)} chunks")
    else:
        logger.error(f"[RETRIEVAL] Failed to get metadata: {result.get('error')}")
    
    return result


@mcp.tool
def retrieve_document_v2(
    document_id: str,
    query: str,
    top_k: int = 5,
    user_id: str = "",
    filters: dict | None = None,
) -> dict:
    """
    LangChain-native equivalent of retrieve_document: hybrid retrieval
    (dense Chroma similarity + BM25 keyword search, fused with
    EnsembleRetriever) followed by cross-encoder reranking down to `top_k`.

    Reads the v2 collection created by upload_and_index_document_v2 (the
    indexing server's LangChain pipeline) — a document indexed only with
    v1's upload_and_index_document will not be found here; use
    retrieve_document for that. This performs retrieval only, no answer
    synthesis.

    Each returned chunk includes a `grounding` block
    (document_id/document_name/source/location/chunk_id/chunk_index/
    content_type/chunk_content_type/section/expanded) in addition to
    `text`/`score`/`metadata`, so a caller can cite exactly where an answer
    came from without re-deriving it from raw metadata. By default adjacent
    chunks are spliced into `text` for extra surrounding context
    (`grounding.expanded` says whether that happened for a given chunk;
    controlled by settings.rag_v2.neighbor_expansion/neighbor_window).

    `filters`: optional exact-match metadata filter applied to both the
    dense and BM25 legs before ranking. Every chunk is tagged
    chunk_content_type="table" or "prose" at index time, so if the user
    asks for tables specifically (e.g. "show me the tables in this doc",
    "what numbers are in the pricing table", "list only tabular data"),
    pass filters={"chunk_content_type": "table"} to search only table
    chunks and skip prose entirely — do this instead of a plain query when
    the user's intent is clearly table-only. Other filter keys: {"page": 3}
    or {"h1": "Results"} for one section. Call get_table_of_contents_v2
    first to see what sections/pages exist before filtering on them.

    `top_k` is clamped to settings.rag_v2.max_top_k.

    `user_id` is always injected by the calling application (never asked of
    the end user) and is checked against the v2 collection's owner — a
    mismatch returns the same "not found" error as a nonexistent
    document_id, exactly like retrieve_document.
    """
    logger.info(f"[RETRIEVAL_V2] retrieve_document_v2 called")
    logger.info(f"[RETRIEVAL_V2] user_id: {user_id}, document_id: {document_id}, query: {query}, top_k: {top_k}, filters: {filters}")

    if not query or not query.strip():
        logger.error("[RETRIEVAL_V2] Query is empty or None")
        return {
            "success": False,
            "document_id": document_id,
            "error": "Query parameter is required and cannot be empty"
        }

    result = retriever_v2.retrieve(
        document_id=document_id,
        query=query,
        top_k=top_k,
        user_id=user_id or None,
        filters=filters or None,
    )

    if result.get("success"):
        logger.info(f"[RETRIEVAL_V2] Retrieved {result.get('chunks_count', 0)} chunks (mode={result.get('retrieval_mode')}, reranked={result.get('reranked')})")
    else:
        logger.error(f"[RETRIEVAL_V2] Retrieval failed: {result.get('error')}")

    return result


@mcp.tool
def get_table_of_contents_v2(document_id: str, user_id: str = "") -> dict:
    """
    A document's section outline — h1/h2/h3 headings captured at index time
    from PDF/docx/txt structure, each with the page it was seen on.

    Call this before retrieve_document_v2 when you want to target a
    specific section or table rather than a blind top-k query: read the
    outline, then pass e.g. filters={"h1": "<heading>"} or
    filters={"page": N} to retrieve_document_v2 to search only that part of
    the document.

    Falls back to page/sheet coverage (same as get_document_info_v2) for
    documents with no captured outline (csv/xlsx, pptx, or one indexed
    before this tool existed) — in that case `toc` is an empty list and
    `pages`/`sheets` are populated instead.

    `user_id` is always injected by the calling application and checked
    against the v2 collection's owner (see retrieve_document_v2's
    docstring).

    Args:
        document_id: UUID of the document

    Returns:
        Dictionary with `toc` (list of {level, title, page}) and, when no
        TOC was captured, `pages`/`sheets`.
    """
    logger.info(f"[RETRIEVAL_V2] get_table_of_contents_v2 called for document_id: {document_id}, user_id: {user_id}")

    result = retriever_v2.get_table_of_contents(document_id, user_id=user_id or None)

    if result.get("success"):
        logger.info(f"[RETRIEVAL_V2] Retrieved TOC: {len(result.get('toc') or [])} entries")
    else:
        logger.error(f"[RETRIEVAL_V2] Failed to get TOC: {result.get('error')}")

    return result


@mcp.tool
def get_document_info_v2(document_id: str, user_id: str = "") -> dict:
    """
    LangChain-native equivalent of get_document_info: metadata and
    statistics for a document indexed via upload_and_index_document_v2
    (total chunks, document name, content type, and page/sheet coverage
    when available).

    `user_id` is always injected by the calling application and checked
    against the v2 collection's owner (see retrieve_document_v2's
    docstring).

    Args:
        document_id: UUID of the document

    Returns:
        Dictionary with document metadata and statistics
    """
    logger.info(f"[RETRIEVAL_V2] get_document_info_v2 called for document_id: {document_id}, user_id: {user_id}")

    result = retriever_v2.get_document_info(document_id, user_id=user_id or None)

    if result.get("success"):
        logger.info(f"[RETRIEVAL_V2] Retrieved metadata: {result.get('total_chunks', 0)} chunks")
    else:
        logger.error(f"[RETRIEVAL_V2] Failed to get metadata: {result.get('error')}")

    return result


@mcp.tool
def get_document_chunks_v2(
    document_id: str,
    user_id: str = "",
    chunk_id: str = "",
    page: int | None = None,
) -> dict:
    """
    All chunks of a document's v2 collection, in original chunk_index
    order, each with full text + metadata + grounding (chunk_id,
    chunk_content_type, page/sheet/row, h1/h2/h3, ...) — no query,
    scoring, or reranking.

    This is for inspection UIs (a document's chunk browser, citation
    click-through), not for answering questions — retrieve_document_v2's
    hybrid search is how to actually answer something from a document.
    (Registered as an MCP tool so the API server can call it, but not
    offered to the chat agent's own tool list — see streaming_agent.py's
    _INTERNAL_ONLY_TOOLS.)

    `chunk_id` or `page` narrows to one chunk / one page's chunks; leave
    both empty/unset to list the whole document.

    `user_id` is always injected by the calling application and checked
    against the v2 collection's owner (see retrieve_document_v2's
    docstring).

    Args:
        document_id: UUID of the document
        chunk_id: optional exact chunk id to fetch just that one chunk
        page: optional page number to fetch just that page's chunks

    Returns:
        Dictionary with `chunks` (list of {text, metadata, grounding}) and
        `chunks_count`.
    """
    logger.info(f"[RETRIEVAL_V2] get_document_chunks_v2 called for document_id: {document_id}, chunk_id: {chunk_id!r}, page: {page}")

    result = retriever_v2.list_chunks(
        document_id=document_id,
        user_id=user_id or None,
        chunk_id=chunk_id or None,
        page=page,
    )

    if result.get("success"):
        logger.info(f"[RETRIEVAL_V2] Listed {result.get('chunks_count', 0)} chunks")
    else:
        logger.error(f"[RETRIEVAL_V2] list_chunks failed: {result.get('error')}")

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
@mcp.tool
def web_search(query: str, max_results: int | None = None) -> dict:
    """
    Search the public web (DuckDuckGo) and return matching results.

    Use this for questions about current events, facts outside the user's
    uploaded documents, or anything retrieve_document_v2 can't answer
    because it isn't in an indexed document. Not a substitute for
    retrieve_document_v2 when the user is asking about a document they
    uploaded — check indexed documents first for those.

    Returns `{success, query, results, results_count}`, where each result
    is `{title, url, snippet}` in the backend's own relevance order. A
    snippet is often too short to fully answer from — call fetch_page on a
    result's `url` to read the actual page when you need more than the
    snippet gives you.

    `max_results` defaults to a server-configured count and is clamped to a
    server-configured ceiling regardless of what's requested.

    Args:
        query: Search query text.
        max_results: Optional cap on how many results to return.

    Returns:
        Dictionary with search results, or `{success: False, error,
        error_category}` on failure (e.g. empty query, backend rate limit).
    """
    logger.info(f"[WEB_SEARCH] web_search called query={query!r} max_results={max_results}")

    result = _web_search(query, max_results, settings.retrieval_server)

    if result.get("success"):
        logger.info(f"[WEB_SEARCH] Returned {result.get('results_count', 0)} results")
    else:
        logger.error(f"[WEB_SEARCH] Failed: {result.get('error')}")

    return result


@mcp.tool
def fetch_page(url: str) -> dict:
    """
    Download a web page and return its main readable text (nav/ads/scripts
    stripped out).

    Use this after web_search when a result's snippet isn't enough to
    answer the question — pass that result's `url` here to read the actual
    page content. Only fetches `http(s)` URLs, and rejects any URL whose
    host resolves to a loopback/private/link-local address (it cannot be
    used to reach internal services).

    Returns `{success, url, title, content, truncated}` — `content` is
    capped to a server-configured length (`truncated` says whether it was
    cut off). On failure returns `{success: False, error, error_category}`
    (e.g. the page couldn't be fetched, was too large, or the URL was
    disallowed).

    Args:
        url: The page URL to fetch (from a web_search result, or one the
            user gave you directly).

    Returns:
        Dictionary with the extracted page text, or a failure envelope.
    """
    logger.info(f"[WEB_SEARCH] fetch_page called url={url!r}")

    result = _fetch_page(url, settings.retrieval_server)

    if result.get("success"):
        logger.info(f"[WEB_SEARCH] Fetched page, content_chars={len(result.get('content', ''))} truncated={result.get('truncated')}")
    else:
        logger.error(f"[WEB_SEARCH] Failed: {result.get('error')}")

    return result


if __name__ == "__main__":
    # MCP_TRANSPORT=stdio lets this server be launched as a subprocess
    # (e.g. from servers.docker.json) instead of a standalone HTTP service —
    # see backend/apps/client/servers.docker.json and the root Dockerfile.
    if os.environ.get("MCP_TRANSPORT", "http") == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="http",
            host=settings.retrieval_server.host,
            port=settings.retrieval_server.port,
        )