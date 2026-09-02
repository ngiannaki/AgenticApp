"""
Google Document AI-based Document Parser for RAG Pipeline
==========================================================
Wraps Google Cloud Document AI to produce the same ``PageContent`` output
as the Docling-based ``HierarchicalParser``, so the rest of the pipeline
(chunker, indexer, retriever) is completely unchanged.

Activated when ``USE_GOOGLE_API = True`` in config/settings.py.

Google DocAI bounding boxes are *normalised* (0–1 range, origin top-left):
    xMin, yMin, xMax, yMax
We map these to our ``BoundingBox(x, y, width, height, coordinate_type="normalized")``.
"""

from __future__ import annotations

import io
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from llama_index.core import Document

from .document_parser import (
    BoundingBox,
    ElementType,
    PageContent,
    ParsedElement,
    ParsedTable,
    TableCell,
    _FallbackParser,
)

logger = logging.getLogger(__name__)

DOCAI_MAX_PAGES_PER_REQUEST = 30


class DocAIPageLimitExceededError(RuntimeError):
    """Raised when DocAI rejects a request due to page-count limits."""

    def __init__(self, page_limit: int, pages_sent: int, message: str):
        super().__init__(message)
        self.page_limit = page_limit
        self.pages_sent = pages_sent

# ── Google credential bootstrap (mirrors llms.py logic) ───────────────────
_DEFAULT_ADC = (
    Path(__file__).resolve().parents[3] / "google" / "application_default_credentials.json"
)
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and _DEFAULT_ADC.exists():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_DEFAULT_ADC)

_DOCAI_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_docai_client(location: str, key_file_path: Optional[str] = None):
    """Create an authenticated Document AI client."""
    try:
        import google.auth
        from google.cloud import documentai
    except ImportError as exc:
        raise ImportError(
            "google-cloud-documentai is required for GoogleDocumentParser. "
            "Install it with: pip install google-cloud-documentai"
        ) from exc

    cred_path = key_file_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and os.path.exists(cred_path):
        credentials, _ = google.auth.load_credentials_from_file(
            cred_path, scopes=_DOCAI_SCOPES
        )
    else:
        credentials, _ = google.auth.default(scopes=_DOCAI_SCOPES)

    opts = {"api_endpoint": f"{location}-documentai.googleapis.com"}
    return documentai.DocumentProcessorServiceClient(
        credentials=credentials, client_options=opts
    )


def _normalized_bbox(bounding_poly) -> Dict[str, float]:
    """Convert a Document AI BoundingPoly to {xMin, yMin, xMax, yMax}."""
    xs = [v.x for v in bounding_poly.normalized_vertices]
    ys = [v.y for v in bounding_poly.normalized_vertices]
    if not xs or not ys:
        return {"xMin": 0.0, "yMin": 0.0, "xMax": 0.0, "yMax": 0.0}
    return {
        "xMin": float(min(xs)),
        "yMin": float(min(ys)),
        "xMax": float(max(xs)),
        "yMax": float(max(ys)),
    }


def _google_bbox_to_bbox(raw: Dict[str, float], page_no: int) -> BoundingBox:
    """Convert a {xMin, yMin, xMax, yMax} dict to our BoundingBox."""
    x = raw["xMin"]
    y = raw["yMin"]
    return BoundingBox(
        x=x,
        y=y,
        width=max(0.0, raw["xMax"] - x),
        height=max(0.0, raw["yMax"] - y),
        page=page_no,
        coordinate_type="normalized",
    )


def _extract_text_from_layout(layout, full_text: str) -> str:
    """Reconstruct text from a Document AI layout's text segments."""
    if not layout or not layout.text_anchor:
        return ""
    chunks: List[str] = []
    for seg in layout.text_anchor.text_segments:
        start = int(seg.start_index)
        end = int(seg.end_index)
        chunks.append(full_text[start:end])
    return "".join(chunks).strip()


def _is_likely_heading(text: str) -> bool:
    """
    Heuristic: short, no trailing period, not numeric-only → probable heading.
    Layout Parser processors also surface headings via visual_elements; this
    heuristic handles standard OCR processors.
    """
    t = text.strip()
    if not t or len(t) > 120:
        return False
    if t.endswith(".") or t.endswith(","):
        return False
    word_count = len(t.split())
    return 1 <= word_count <= 12


def _build_markdown_from_rows(rows: List[List[str]], headers: List[str]) -> str:
    """Build a GFM markdown table string from header + data rows."""
    if not headers:
        return ""
    sep = "| " + " | ".join(["---"] * len(headers)) + " |"
    header_row = "| " + " | ".join(headers) + " |"
    body_rows = [
        "| " + " | ".join(row) + " |"
        for row in rows
        if row  # skip empty rows
    ]
    return "\n".join([header_row, sep] + body_rows)


def _extract_table(docai_table, full_text: str, page_no: int) -> ParsedTable:
    """
    Convert a Document AI Table object (from ``page.tables``) into
    our ``ParsedTable`` with full cell-level data.
    """
    bbox = _google_bbox_to_bbox(
        _normalized_bbox(docai_table.layout.bounding_poly), page_no
    )

    def _row_to_texts(row) -> List[str]:
        return [_extract_text_from_layout(cell.layout, full_text) for cell in row.cells]

    header_rows_texts: List[List[str]] = [
        _row_to_texts(r) for r in docai_table.header_rows
    ]
    body_rows_texts: List[List[str]] = [
        _row_to_texts(r) for r in docai_table.body_rows
    ]

    headers = header_rows_texts[0] if header_rows_texts else []
    all_rows = header_rows_texts + body_rows_texts

    # Build TableCell objects
    cells: List[TableCell] = []
    for ri, row in enumerate(all_rows):
        for ci, cell_text in enumerate(row):
            cells.append(
                TableCell(
                    row=ri,
                    col=ci,
                    text=cell_text,
                    is_header=(ri < len(header_rows_texts)),
                )
            )

    markdown = _build_markdown_from_rows(body_rows_texts, headers)

    return ParsedTable(
        markdown=markdown,
        rows=all_rows,
        cells=cells,
        headers=headers,
        num_rows=len(all_rows),
        num_cols=len(headers) if headers else (len(all_rows[0]) if all_rows else 0),
        bounding_box=bbox,
    )


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class GoogleDocumentParser:
    """
    Google Document AI-powered document parser.

    Produces the same ``List[PageContent]`` as the Docling-based
    ``HierarchicalParser`` so the rest of the RAG pipeline is unchanged.

    Parameters
    ----------
    project_id      : GCP project ID
    location        : DocAI processor region ("eu" or "us")
    processor_id    : Document AI processor ID
    key_file_path   : Optional path to service-account / ADC JSON key file.
                      Falls back to GOOGLE_APPLICATION_CREDENTIALS env var.
    enable_hierarchical : Whether to annotate heading hierarchy on elements.
    """

    def __init__(
        self,
        project_id: str,
        location: str = "eu",
        processor_id: str = "",
        key_file_path: Optional[str] = None,
        enable_hierarchical: bool = True,
    ) -> None:
        self.project_id = project_id
        self.location = location
        self.processor_id = processor_id
        self.key_file_path = key_file_path
        self.enable_hierarchical = enable_hierarchical
        self._fallback = _FallbackParser()
        logger.info(
            "GoogleDocumentParser initialised (project=%s location=%s processor=%s)",
            project_id,
            location,
            processor_id,
        )

    # ------------------------------------------------------------------
    # Public API  (mirrors HierarchicalParser / DocumentParser)
    # ------------------------------------------------------------------

    def parse_file(self, file_path: str | Path) -> List[PageContent]:
        """
        Parse a PDF from disk using Google Document AI.

        Returns
        -------
        List[PageContent], one per page — identical structure to Docling output.
        """
        file_path = Path(file_path)
        ext = file_path.suffix.lower()

        if ext != ".pdf":
            # For non-PDF files fall back to the regex parser; Google DocAI is
            # optimised for PDFs and image documents.
            logger.info(
                "GoogleDocumentParser: non-PDF file (%s), using fallback regex parser.",
                file_path.name,
            )
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
                pages = [self._fallback.parse_page(text, 1)]
            except Exception as exc:
                logger.error(
                    "Failed to read %s for fallback parsing: %s", file_path, exc, exc_info=True
                )
                pages = [PageContent(page_number=1, text="", markdown="")]
            return pages

        try:
            pages = self._parse_pdf_with_docai(file_path)
        except Exception as exc:
            logger.error(
                "Google DocAI parsing failed for %s: %s. Falling back to regex parser.",
                file_path,
                exc,
                exc_info=True,
            )
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
                pages = [self._fallback.parse_page(text, 1)]
            except Exception:
                pages = [PageContent(page_number=1, text="", markdown="")]

        if self.enable_hierarchical:
            pages = self._annotate_hierarchy(pages)
        return pages

    def parse_document(self, document: Document) -> List[PageContent]:
        """
        Parse a LlamaIndex Document.

        If ``document.metadata`` contains ``file_path`` pointing to a PDF on
        disk, Google DocAI is used. Otherwise falls back to the regex parser.
        """
        file_path_str = (document.metadata or {}).get("file_path")
        if file_path_str:
            fp = Path(file_path_str)
            if fp.exists() and fp.suffix.lower() == ".pdf":
                pages = self.parse_file(fp)
                target_page = (document.metadata or {}).get("page")
                if target_page is not None:
                    pages = [p for p in pages if p.page_number == int(target_page)]
                    if not pages:
                        pages = [self._fallback.parse_page(document.text, int(target_page))]
                return pages

        # fallback: parse embedded text
        raw_pages = re.split(r"\n\s*\f\s*\n|\n---\n|\n\*\*\*\n", document.text)
        base_page = int((document.metadata or {}).get("page", 1))
        result: List[PageContent] = []
        for idx, page_text in enumerate(raw_pages):
            page_text = page_text.strip()
            if page_text:
                result.append(self._fallback.parse_page(page_text, base_page + idx))
        return result

    # ------------------------------------------------------------------
    # Google DocAI internals
    # ------------------------------------------------------------------

    def _parse_pdf_with_docai(self, file_path: Path) -> List[PageContent]:
        """Call the Document AI API and convert the result to PageContent list."""
        from google.cloud import documentai  # noqa: PLC0415  (lazy import)

        client = _make_docai_client(self.location, self.key_file_path)
        processor_name = client.processor_path(
            self.project_id, self.location, self.processor_id
        )

        with open(file_path, "rb") as fh:
            pdf_bytes = fh.read()

        total_pages = self._count_pdf_pages(pdf_bytes)
        batch_size = min(total_pages, DOCAI_MAX_PAGES_PER_REQUEST)

        while True:
            if total_pages <= batch_size:
                document = self._process_docai_request(
                    client=client,
                    documentai=documentai,
                    processor_name=processor_name,
                    pdf_bytes=pdf_bytes,
                    file_name=file_path.name,
                    batch_label=None,
                )
                full_text: str = document.text
                logger.info("DocAI response received — %d pages", len(document.pages))
                return self._convert_docai_document(document, full_text)

            logger.info(
                "Splitting %s into %d-page Google DocAI batches (%d pages total)",
                file_path.name,
                batch_size,
                total_pages,
            )

            batches = self._split_pdf_bytes(pdf_bytes, batch_size)
            pages: List[PageContent] = []
            page_offset = 0
            try:
                for batch_index, batch_bytes in enumerate(batches, start=1):
                    document = self._process_docai_request(
                        client=client,
                        documentai=documentai,
                        processor_name=processor_name,
                        pdf_bytes=batch_bytes,
                        file_name=file_path.name,
                        batch_label=f"batch {batch_index}/{len(batches)}",
                    )
                    full_text = document.text
                    logger.info(
                        "DocAI response received for %s batch %d/%d — %d pages",
                        file_path.name,
                        batch_index,
                        len(batches),
                        len(document.pages),
                    )
                    pages.extend(
                        self._convert_docai_document(
                            document,
                            full_text,
                            page_number_offset=page_offset,
                        )
                    )
                    page_offset += len(document.pages)

                return pages
            except DocAIPageLimitExceededError as exc:
                if exc.page_limit >= batch_size:
                    raise
                logger.warning(
                    "DocAI page limit exceeded at batch size %d (%s). Retrying with %d-page batches.",
                    batch_size,
                    exc,
                    exc.page_limit,
                )
                batch_size = max(1, exc.page_limit)

    def _process_docai_request(
        self,
        client: Any,
        documentai: Any,
        processor_name: str,
        pdf_bytes: bytes,
        file_name: str,
        batch_label: Optional[str],
    ) -> Any:
        """Submit a single PDF payload to DocAI and return the parsed document."""
        raw_doc = documentai.RawDocument(content=pdf_bytes, mime_type="application/pdf")
        request = documentai.ProcessRequest(name=processor_name, raw_document=raw_doc)

        if batch_label:
            logger.info(
                "Sending %s %s to Google DocAI (%d bytes)…",
                file_name,
                batch_label,
                len(pdf_bytes),
            )
        else:
            logger.info("Sending %s to Google DocAI (%d bytes)…", file_name, len(pdf_bytes))

        try:
            result = client.process_document(request=request)
        except Exception as exc:
            page_limit = self._extract_page_limit_from_error(exc)
            if page_limit is not None:
                pages_sent = self._count_pdf_pages(pdf_bytes)
                raise DocAIPageLimitExceededError(
                    page_limit=page_limit,
                    pages_sent=pages_sent,
                    message=str(exc),
                ) from exc
            raise
        return result.document

    @staticmethod
    def _extract_page_limit_from_error(exc: Exception) -> Optional[int]:
        """Parse DocAI PAGE_LIMIT_EXCEEDED errors and return the allowed page limit."""
        message = str(exc)
        if "PAGE_LIMIT_EXCEEDED" not in message and "exceed the limit" not in message:
            return None

        patterns = [
            r"non-imageless mode exceed the limit:\s*(\d+)\s*got\s*(\d+)",
            r"pages exceed the limit:\s*(\d+)\s*got\s*(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, message, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))

        meta_match = re.search(r'key:\s*"page_limit"\s*value:\s*"(\d+)"', message)
        if meta_match:
            meta_limit = int(meta_match.group(1))
            non_imageless_match = re.search(
                r"non-imageless mode exceed the limit:\s*(\d+)",
                message,
                flags=re.IGNORECASE,
            )
            if non_imageless_match:
                return min(meta_limit, int(non_imageless_match.group(1)))
            return meta_limit

        return None

    def _count_pdf_pages(self, pdf_bytes: bytes) -> int:
        """Count pages in a PDF payload so we can stay within DocAI limits."""
        import PyPDF2  # noqa: PLC0415

        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        return len(reader.pages)

    def _split_pdf_bytes(self, pdf_bytes: bytes, batch_size: int) -> List[bytes]:
        """Split a PDF into smaller PDF byte blobs with at most batch_size pages each."""
        import PyPDF2  # noqa: PLC0415

        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        batches: List[bytes] = []
        for start in range(0, len(reader.pages), batch_size):
            writer = PyPDF2.PdfWriter()
            for page_index in range(start, min(start + batch_size, len(reader.pages))):
                writer.add_page(reader.pages[page_index])
            output = io.BytesIO()
            writer.write(output)
            batches.append(output.getvalue())
        return batches

    def _convert_docai_document(
        self,
        document: Any,
        full_text: str,
        page_number_offset: int = 0,
    ) -> List[PageContent]:
        """Convert a raw DocAI Document proto to our List[PageContent]."""
        pages: List[PageContent] = []

        for local_page_num, page in enumerate(document.pages, start=1):
            page_num = page_number_offset + local_page_num
            page_width = float(page.dimension.width) if page.dimension else 0.0
            page_height = float(page.dimension.height) if page.dimension else 0.0

            elements: List[ParsedElement] = []
            tables: List[ParsedTable] = []

            # ── Tables ────────────────────────────────────────────────────
            # Track table bboxes to avoid double-emitting table text as paragraphs.
            table_bboxes: List[Dict[str, float]] = []
            for docai_table in page.tables:
                try:
                    parsed_table = _extract_table(docai_table, full_text, page_num)
                    tables.append(parsed_table)
                    table_bboxes.append(
                        _normalized_bbox(docai_table.layout.bounding_poly)
                    )
                    elements.append(
                        ParsedElement(
                            type=ElementType.TABLE,
                            content=parsed_table.markdown,
                            bounding_box=parsed_table.bounding_box,
                            metadata={
                                "num_rows": parsed_table.num_rows,
                                "num_cols": parsed_table.num_cols,
                            },
                            table=parsed_table,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to extract table on page %d: %s", page_num, exc, exc_info=True
                    )

            # ── Paragraphs / text blocks ──────────────────────────────────
            para_source = page.paragraphs if page.paragraphs else page.blocks
            for para in para_source:
                text = _extract_text_from_layout(para.layout, full_text)
                if not text:
                    continue

                raw_bbox = _normalized_bbox(para.layout.bounding_poly)

                # Skip text that belongs to an already-extracted table
                if self._overlaps_any_table(raw_bbox, table_bboxes):
                    continue

                bbox = _google_bbox_to_bbox(raw_bbox, page_num)
                etype = (
                    ElementType.HEADING if _is_likely_heading(text) else ElementType.TEXT
                )
                content = (
                    f"## {text}" if etype == ElementType.HEADING else text
                )
                elements.append(
                    ParsedElement(
                        type=etype,
                        content=content,
                        bounding_box=bbox,
                    )
                )

            # ── Visual / figure elements ──────────────────────────────────
            for visual in page.visual_elements:
                text = _extract_text_from_layout(visual.layout, full_text)
                raw_bbox = _normalized_bbox(visual.layout.bounding_poly)
                bbox = _google_bbox_to_bbox(raw_bbox, page_num)
                elements.append(
                    ParsedElement(
                        type=ElementType.FIGURE,
                        content=text or "[Figure]",
                        bounding_box=bbox,
                    )
                )

            # ── Build markdown and plain text ─────────────────────────────
            md_parts = [elem.content for elem in elements if elem.content]
            markdown = "\n\n".join(md_parts)
            plain_text = re.sub(r"```.*?```", "", markdown, flags=re.DOTALL)
            plain_text = re.sub(r"#{1,6} ", "", plain_text).strip()

            pages.append(
                PageContent(
                    page_number=page_num,
                    text=plain_text,
                    markdown=markdown,
                    elements=elements,
                    tables=tables,
                    has_tables=len(tables) > 0,
                    width=page_width or None,
                    height=page_height or None,
                )
            )

        return pages

    # ------------------------------------------------------------------
    # Hierarchy annotation (identical logic to HierarchicalParser)
    # ------------------------------------------------------------------

    def _annotate_hierarchy(self, pages: List[PageContent]) -> List[PageContent]:
        """Propagate heading hierarchy to all element metadata dicts."""
        h1 = h2 = h3 = None
        for page in pages:
            for elem in page.elements:
                if elem.type == ElementType.HEADING:
                    c = elem.content
                    if c.startswith("# "):
                        h1, h2, h3 = c, None, None
                    elif c.startswith("## "):
                        h2, h3 = c, None
                    elif c.startswith("### "):
                        h3 = c
                elem.metadata = elem.metadata or {}
                elem.metadata["hierarchy"] = {"h1": h1, "h2": h2, "h3": h3}
        return pages

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _overlaps_any_table(
        bbox: Dict[str, float], table_bboxes: List[Dict[str, float]], threshold: float = 0.5
    ) -> bool:
        """
        Return True if ``bbox`` overlaps any known table bbox by more than
        ``threshold`` of its own area (so we don't double-emit table text).
        """
        xMin, yMin, xMax, yMax = (
            bbox["xMin"], bbox["yMin"], bbox["xMax"], bbox["yMax"]
        )
        area = max(0.0, xMax - xMin) * max(0.0, yMax - yMin)
        if area == 0.0:
            return False

        for tb in table_bboxes:
            ix1 = max(xMin, tb["xMin"])
            iy1 = max(yMin, tb["yMin"])
            ix2 = min(xMax, tb["xMax"])
            iy2 = min(yMax, tb["yMax"])
            inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
            if inter / area >= threshold:
                return True
        return False
