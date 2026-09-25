"""Structure-aware PDF loading + markdown splitting for the v2 indexing
pipeline.

Two things plain LangChain loaders + a flat RecursiveCharacterTextSplitter
lose: table structure (a table becomes run-on text, and the character
splitter can cut it mid-row) and section structure (no heading survives into
chunk metadata, so a chunk can't be cited by section or filtered on).

This module fixes both:
- `pdf_to_documents` renders a PDF as per-page markdown (headings as `#...`,
  tables as `| ... |`) via one of two engines (settings.rag_v2.pdf_engine),
  instead of plain-text extraction.
- `split_markdown_preserving_structure` splits any markdown-flavored
  Document (PDF, docx, txt/md) so that table blocks stay atomic and prose is
  split on headings first (capturing h1/h2/h3 into metadata) before falling
  back to a character-bounded split within each section.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from langchain_core.documents import Document

from backend.config import settings

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
# A GFM table separator row, pipe-wrapped ("|---|---|") or not ("--- | ---")
# — parsers/docx_reader.py's _to_markdown_table renders the latter, PDF
# engines (docling/pymupdf4llm) render the former.
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


# --------------------------------------------------------------------------
# PDF -> markdown Documents
# --------------------------------------------------------------------------

_docling_parser = None
_docling_ready: Optional[bool] = None
_docling_ocr_parser = None
_docling_ocr_ready: Optional[bool] = None


def _get_docling_parser(use_ocr: bool = False):
    """Lazily construct and cache the Docling-backed HierarchicalParser
    (same lazy-singleton pattern as parsers/pdf_markdown.py's _get_parser,
    since constructing it loads Docling's model pipeline).

    Docling bakes `do_ocr` into the pipeline at construction time, so the
    OCR and non-OCR converters are two independent singletons rather than
    one reconfigurable instance."""
    global _docling_parser, _docling_ready, _docling_ocr_parser, _docling_ocr_ready

    if use_ocr:
        if _docling_ocr_ready is None:
            try:
                from backend.mcp_servers.indexing.parsers.document_parser import HierarchicalParser

                _docling_ocr_parser = HierarchicalParser(enable_hierarchical=True, use_ocr=True)
                _docling_ocr_ready = _docling_ocr_parser._converter is not None
                if not _docling_ocr_ready:
                    logger.warning("[LOADER_V2] Docling OCR converter did not initialize; scanned-PDF extraction unavailable")
            except Exception as exc:
                logger.warning(f"[LOADER_V2] Docling OCR unavailable for scanned PDFs: {exc}")
                _docling_ocr_parser = None
                _docling_ocr_ready = False
        return _docling_ocr_parser if _docling_ocr_ready else None

    if _docling_ready is None:
        try:
            from backend.mcp_servers.indexing.parsers.document_parser import HierarchicalParser

            _docling_parser = HierarchicalParser(enable_hierarchical=True, use_ocr=False)
            _docling_ready = _docling_parser._converter is not None
            if not _docling_ready:
                logger.warning("[LOADER_V2] Docling converter did not initialize; PDF structure extraction unavailable")
        except Exception as exc:
            logger.warning(f"[LOADER_V2] Docling unavailable for structured PDF loading: {exc}")
            _docling_parser = None
            _docling_ready = False

    return _docling_parser if _docling_ready else None


def _pdf_has_text_layer(file_path: Path, sample_pages: int = 3) -> bool:
    """Check whether `file_path` has an extractable text layer, sampling the
    first `sample_pages` pages (equivalent to the skill's `pdffonts`
    diagnostic: catches scanned/rasterized PDFs before wasting a parse pass
    on them, so they route straight to OCR instead of silently indexing as
    near-empty documents)."""
    try:
        import pymupdf
    except ImportError:
        return True  # can't check without the library; let the normal engine try

    try:
        doc = pymupdf.open(str(file_path))
        try:
            for i, page in enumerate(doc):
                if i >= sample_pages:
                    break
                if page.get_text().strip():
                    return True
            return False
        finally:
            doc.close()
    except Exception as exc:
        logger.warning(f"[LOADER_V2] Text-layer check failed for {file_path.name}, assuming it has text: {exc}")
        return True


def _pdf_to_documents_docling(file_path: Path, use_ocr: bool = False) -> list[Document]:
    parser = _get_docling_parser(use_ocr=use_ocr)
    if parser is None:
        raise RuntimeError("Docling OCR parser unavailable" if use_ocr else "Docling parser unavailable")

    pages = parser.parse_file(file_path)
    docs = []
    for page in pages:
        if not page.markdown or not page.markdown.strip():
            continue
        docs.append(
            Document(
                page_content=page.markdown,
                metadata={"source": file_path.name, "page": page.page_number, "has_tables": page.has_tables},
            )
        )
    return docs


def _pdf_to_documents_pymupdf4llm(file_path: Path) -> list[Document]:
    import pymupdf4llm

    pages = pymupdf4llm.to_markdown(str(file_path), page_chunks=True)
    docs = []
    for page in pages:
        text = page.get("text", "")
        if not text.strip():
            continue
        metadata = page.get("metadata") or {}
        raw_page = metadata.get("page")
        # pymupdf4llm's per-page metadata["page"] is already 1-based.
        page_number = raw_page if isinstance(raw_page, int) else len(docs) + 1
        has_tables = bool(page.get("tables"))
        docs.append(
            Document(
                page_content=text,
                metadata={"source": file_path.name, "page": page_number, "has_tables": has_tables},
            )
        )
    return docs


def pdf_to_documents(file_path: Path) -> list[Document]:
    """Render `file_path` as per-page markdown Documents using the
    configured engine (settings.rag_v2.pdf_engine), falling back to plain
    PyMuPDF text extraction if the engine is unavailable or fails.

    Scanned/rasterized PDFs (no text layer) are detected up front and routed
    to Docling's OCR pipeline instead — pymupdf4llm has no OCR, so without
    this check a scan silently produces near-empty pages that get filtered
    out downstream, and the document ends up indexed with little or no
    content and no error."""
    if not _pdf_has_text_layer(file_path):
        logger.info(f"[LOADER_V2] {file_path.name} has no extractable text layer — treating as scanned, using Docling OCR")
        try:
            docs = _pdf_to_documents_docling(file_path, use_ocr=True)
            if docs:
                return docs
            logger.warning(f"[LOADER_V2] OCR produced no pages for {file_path.name}, falling back to plain text extraction")
        except Exception as exc:
            logger.warning(f"[LOADER_V2] OCR failed for {file_path.name}, falling back to plain text extraction: {exc}")

        from backend.mcp_servers.indexing.langchain_loaders import _fallback_pdf

        return _fallback_pdf(file_path)

    engine = settings.rag_v2.pdf_engine
    try:
        if engine == "pymupdf4llm":
            docs = _pdf_to_documents_pymupdf4llm(file_path)
        else:
            docs = _pdf_to_documents_docling(file_path)
        if docs:
            return docs
        logger.warning(f"[LOADER_V2] PDF engine {engine!r} produced no pages for {file_path.name}, falling back to plain text")
    except Exception as exc:
        logger.warning(f"[LOADER_V2] PDF engine {engine!r} failed for {file_path.name}, falling back to plain text: {exc}")

    from backend.mcp_servers.indexing.langchain_loaders import _fallback_pdf

    return _fallback_pdf(file_path)


# --------------------------------------------------------------------------
# Markdown splitting: table-atomic + header-aware
# --------------------------------------------------------------------------


def _split_table_by_rows(table_text: str, max_chars: int) -> list[str]:
    """Split an over-long markdown table into row-batches, repeating the
    header (+ separator) row at the top of every batch."""
    lines = [ln for ln in table_text.splitlines() if ln.strip()]
    if len(lines) <= 2:
        return [table_text]

    header_lines = lines[:2]  # header row + `---` separator row
    data_lines = lines[2:]
    header_block = "\n".join(header_lines)

    batches: list[str] = []
    current: list[str] = []
    current_len = len(header_block)
    for line in data_lines:
        if current and current_len + len(line) + 1 > max_chars:
            batches.append(header_block + "\n" + "\n".join(current))
            current = []
            current_len = len(header_block)
        current.append(line)
        current_len += len(line) + 1
    if current:
        batches.append(header_block + "\n" + "\n".join(current))

    return batches or [table_text]


def _extract_table_blocks(text: str) -> list[tuple[str, str]]:
    """Split `text` into an ordered list of (kind, chunk) segments, where
    kind is 'table' for a contiguous markdown table block and 'text'
    otherwise.

    A table is recognized by its separator row (`_TABLE_SEP_RE`, e.g.
    `|---|---|` or `--- | ---`) immediately following a header row that
    contains `|` — the one part of GFM table syntax that's unambiguous
    regardless of whether rows are pipe-wrapped (docling/pymupdf4llm) or not
    (parsers/docx_reader.py). Once found, subsequent contiguous `|`-bearing
    lines are absorbed as data rows.
    """
    lines = text.split("\n")
    n = len(lines)
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    i = 0
    while i < n:
        line = lines[i]
        is_separator = (
            i > 0
            and "|" in lines[i - 1]
            and lines[i - 1].strip()
            and bool(_TABLE_SEP_RE.match(line.strip()))
        )
        if is_separator:
            header_line = buf.pop() if buf and buf[-1] == lines[i - 1] else lines[i - 1]
            if buf:
                segments.append(("text", "\n".join(buf)))
                buf = []
            table_lines = [header_line, line]
            i += 1
            while i < n and lines[i].strip() and "|" in lines[i]:
                table_lines.append(lines[i])
                i += 1
            segments.append(("table", "\n".join(table_lines)))
            continue
        buf.append(line)
        i += 1
    if buf:
        segments.append(("text", "\n".join(buf)))
    return segments or [("text", text)]


def _extract_headings(text: str) -> list[dict]:
    headings = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            headings.append({"level": len(m.group(1)), "title": m.group(2).strip()})
    return headings


def _update_heading_state(state: dict, headings: list[dict]) -> None:
    """Track the current h1/h2/h3 as headings are encountered in document
    order, resetting deeper levels when a shallower one changes — same rule
    parsers/document_parser.py's HierarchicalParser uses for its hierarchy
    annotation. Used to tag a table chunk (which has no heading of its own)
    with the section it physically sits under."""
    for h in headings:
        level, title = h["level"], h["title"]
        if level <= 1:
            state["h1"], state["h2"], state["h3"] = title, None, None
        elif level == 2:
            state["h2"], state["h3"] = title, None
        else:
            state["h3"] = title


def split_markdown_preserving_structure(
    doc: Document,
    chunk_size: int,
    chunk_overlap: int,
) -> tuple[list[Document], list[dict]]:
    """Split one markdown-flavored Document into chunks that keep tables
    atomic and carry section (h1/h2/h3) metadata.

    Returns (chunks, headings_seen) — `headings_seen` is the ordered list of
    `{level, title}` headings found in `doc`, annotated with `doc.metadata`'s
    `page` (when present), for the caller to fold into a document-level TOC.
    """
    from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

    text = doc.page_content
    base_metadata = dict(doc.metadata)
    page = base_metadata.get("page")

    headings_seen = [{**h, "page": page} for h in _extract_headings(text)]

    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")],
        strip_headers=True,
    )
    char_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        add_start_index=True,
    )

    out: list[Document] = []
    heading_state: dict = {"h1": None, "h2": None, "h3": None}
    for kind, segment in _extract_table_blocks(text):
        if not segment.strip():
            continue

        if kind == "table":
            section_meta = {k: v for k, v in heading_state.items() if v}
            for table_chunk in _split_table_by_rows(segment, max_chars=chunk_size * 2):
                out.append(
                    Document(
                        page_content=table_chunk,
                        metadata={**base_metadata, **section_meta, "chunk_content_type": "table"},
                    )
                )
            continue

        _update_heading_state(heading_state, _extract_headings(segment))

        try:
            header_docs = header_splitter.split_text(segment)
        except Exception:
            header_docs = [Document(page_content=segment, metadata={})]

        for hdoc in header_docs:
            section_meta = {**base_metadata, **hdoc.metadata, "chunk_content_type": "prose"}
            breadcrumb = " > ".join(
                section_meta[k] for k in ("h1", "h2", "h3") if section_meta.get(k)
            )
            for sub in char_splitter.split_documents([Document(page_content=hdoc.page_content, metadata=section_meta)]):
                if breadcrumb and sub.page_content.strip():
                    sub.page_content = f"Section: {breadcrumb}\n\n{sub.page_content}"
                if sub.page_content.strip():
                    out.append(sub)

    return out, headings_seen
