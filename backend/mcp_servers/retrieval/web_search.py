"""External web search + page fetch, backing the retrieval server's
web_search and fetch_page tools.

Separate from retriever.py/langchain_pipeline.py on purpose: those query
this backend's own indexed documents (Chroma), while this module reaches
out to the open web. Same server because both are "get the agent
information it doesn't already have" — web_search is just an external
source alongside the indexed ones.

No API key: uses the `ddgs` package (DuckDuckGo) for search and `httpx` +
`trafilatura` for page fetch/extraction, per user's no-paid-API-key
preference. That means no SLA and results can be less consistent /
occasionally rate-limited compared to a paid provider (Tavily, Brave,
SerpAPI) — acceptable trade-off for this project's needs.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from backend.config.settings import RetrievalServerSettings


class WebSearchError(Exception):
    """Base class for errors raised by web_search/fetch_page."""

    category = "search_error"


class ValidationError(WebSearchError):
    """Malformed or disallowed input (empty query, bad URL, disallowed scheme)."""

    category = "validation_error"


class UpstreamError(WebSearchError):
    """The search backend or the fetched site failed or timed out."""

    category = "upstream_error"


def _to_error_dict(exc: Exception) -> dict:
    if isinstance(exc, WebSearchError):
        return {"success": False, "error": str(exc), "error_category": exc.category}
    return {"success": False, "error": "An internal search error occurred.", "error_category": "search_error"}


def web_search(query: str, max_results: int | None, settings: RetrievalServerSettings) -> dict:
    """Run a DuckDuckGo search and return `{success, results, results_count}`.

    `results` is a list of `{title, url, snippet}`, in the backend's own
    relevance order (no reranking is applied). Never raises for a bad
    query/count — always returns the `{success: False, error, ...}`
    envelope instead.
    """
    try:
        query = (query or "").strip()
        if not query:
            raise ValidationError("query is required and cannot be empty")
        if len(query) > settings.web_search_max_query_length:
            raise ValidationError(
                f"query is too long ({len(query)} chars, max {settings.web_search_max_query_length})"
            )

        count = max_results if max_results is not None else settings.web_search_default_max_results
        count = max(1, min(count, settings.web_search_max_results_ceiling))

        from ddgs import DDGS  # imported lazily so the server can start even if unused/uninstalled
        from ddgs.exceptions import DDGSException

        try:
            with DDGS(timeout=settings.web_search_timeout_seconds) as ddgs:
                raw_results = list(
                    ddgs.text(
                        query,
                        region=settings.web_search_region,
                        safesearch=settings.web_search_safesearch,
                        max_results=count,
                    )
                )
        except DDGSException as exc:
            raise UpstreamError(f"web search backend failed: {exc}") from exc

        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("href", ""),
                "snippet": item.get("body", ""),
            }
            for item in raw_results
        ]
        return {"success": True, "query": query, "results": results, "results_count": len(results)}
    except Exception as exc:  # noqa: BLE001 - translated centrally, never re-raised raw
        return _to_error_dict(exc)


def _resolves_to_public_address(hostname: str) -> bool:
    """Reject hostnames that resolve to loopback/private/link-local addresses.

    fetch_page takes a caller-supplied URL, so without this check it could
    be used to reach internal services (SSRF) — e.g. a URL pointing at
    localhost or a cloud metadata endpoint (169.254.169.254).
    """
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for family, _, _, _, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return False
    return True


def fetch_page(url: str, settings: RetrievalServerSettings) -> dict:
    """Download `url` and extract its main readable text.

    Returns `{success, url, title, content, truncated}`. Blocks disallowed
    schemes and addresses that resolve to loopback/private/link-local
    ranges (SSRF guard) before making any request. Never raises — always
    returns the `{success: False, error, ...}` envelope instead.
    """
    try:
        url = (url or "").strip()
        if not url:
            raise ValidationError("url is required and cannot be empty")

        parsed = urlparse(url)
        if parsed.scheme not in settings.fetch_page_allowed_schemes:
            raise ValidationError(f"url scheme {parsed.scheme!r} is not allowed")
        if not parsed.hostname:
            raise ValidationError("url has no host")
        if not _resolves_to_public_address(parsed.hostname):
            raise ValidationError("url resolves to a disallowed address")

        try:
            with httpx.Client(follow_redirects=True, timeout=settings.fetch_page_timeout_seconds) as client:
                with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0 (compatible; AgenticAppBot/1.0)"}) as response:
                    response.raise_for_status()
                    content_length = int(response.headers.get("content-length") or 0)
                    if content_length and content_length > settings.fetch_page_max_response_bytes:
                        raise ValidationError("page is too large to fetch")

                    chunks = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > settings.fetch_page_max_response_bytes:
                            raise ValidationError("page is too large to fetch")
                        chunks.append(chunk)
                    html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as exc:
            raise UpstreamError(f"could not fetch page: {exc}") from exc

        import trafilatura  # imported lazily, same rationale as ddgs above

        extracted = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
        metadata = trafilatura.extract_metadata(html)
        title = (metadata.title if metadata else "") or ""

        truncated = len(extracted) > settings.fetch_page_max_content_chars
        content = extracted[: settings.fetch_page_max_content_chars]

        return {
            "success": True,
            "url": url,
            "title": title,
            "content": content,
            "truncated": truncated,
        }
    except Exception as exc:  # noqa: BLE001 - translated centrally, never re-raised raw
        return _to_error_dict(exc)
