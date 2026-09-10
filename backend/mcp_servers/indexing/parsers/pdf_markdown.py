"""Docling-backed markdown rendering for PDFs.

pymupdf (parsers/pdf.py) is fast and dependency-light but only gives plain
text. Docling is layout-aware — it detects headings and tables and renders
real markdown (# headers, | tables |) instead of a flat wall of text — so
it's the default for read_document's markdown output. Docling (and the
DocumentParser wrapper in document_parser.py) already degrades gracefully
when unavailable; this module adds one more fallback level on top so a
missing/broken Docling install never breaks read_document, only lowers the
quality of its markdown to plain pymupdf text wrapped with page headers.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_parser = None
_parser_ready: Optional[bool] = None


def _get_parser():
    """Lazily construct and cache the Docling-backed parser.

    Cached as a singleton (module-level) since DocumentParser.__init__
    loads Docling's model pipeline, which is too expensive to redo per call.
    """

    global _parser, _parser_ready

    if _parser_ready is None:
        try:
            from backend.mcp_servers.indexing.parsers.document_parser import DocumentParser
            _parser = DocumentParser(use_ocr=False)
            _parser_ready = _parser._converter is not None
            if not _parser_ready:
                logger.warning("Docling converter did not initialize; PDF markdown will fall back to plain text")
        except Exception as exc:
            logger.warning("Docling unavailable for PDF markdown extraction: %s", exc)
            _parser = None
            _parser_ready = False

    return _parser if _parser_ready else None


def parse_pdf_markdown(pdf_path: str | Path, pages: list[dict]) -> str:
    """Render a PDF as markdown, using Docling when available.

    `pages` is parse_pdf's already-extracted `pages` list (`[{page_number,
    text}, ...]`) — used only as the fallback source if Docling can't
    produce markdown, so callers don't pay for two independent PDF reads.
    """

    parser = _get_parser()

    if parser is not None:
        try:
            docling_pages = parser.parse_file(Path(pdf_path))
            markdown = "\n\n".join(p.markdown for p in docling_pages if p.markdown)
            if markdown.strip():
                return markdown
        except Exception as exc:
            logger.warning("Docling conversion failed for %s, falling back to plain text: %s", pdf_path, exc)

    return "\n\n".join(f"## Page {p['page_number']}\n\n{p['text']}" for p in pages)
