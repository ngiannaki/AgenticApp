"""
AI-Powered Document Parser for RAG Pipeline
============================================
Uses IBM Docling for state-of-the-art document understanding:
  - Per-page markdown extraction (layout-aware)
  - Table detection and extraction (per page, as list-of-dicts + raw markdown)
  - Bounding boxes for every element (text blocks, tables, figures, headings)
  - OCR fallback for scanned PDFs
  - Figure / image captioning metadata

Docling produces a rich DoclingDocument object. We convert that into our
clean, strongly-typed domain objects so the rest of the pipeline is unchanged.
"""

from __future__ import annotations

from typing import List, Dict, Any, Optional
import logging
import re
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path

from llama_index.core import Document

logger = logging.getLogger(__name__)


class ElementType(Enum):
    """Types of elements that can be parsed from documents"""
    TEXT = "text"
    TABLE = "table"
    HEADING = "heading"
    CODE = "code"
    LIST = "list"
    IMAGE = "image"
    FIGURE = "figure"
    CAPTION = "caption"


@dataclass
class BoundingBox:
    """Represents a bounding box in page-space (normalised 0-1 floats or points)"""
    x: float
    y: float
    width: float
    height: float
    page: int
    coordinate_type: str = "normalized"   # "normalized" | "points"


@dataclass
class TableCell:
    row: int
    col: int
    text: str
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False


@dataclass
class ParsedTable:
    """A structured table: raw markdown + row-oriented data"""
    markdown: str
    rows: List[List[str]]         # raw rows
    cells: List[TableCell]        # rich cell objects
    headers: List[str]
    num_rows: int
    num_cols: int
    bounding_box: Optional[BoundingBox] = None
    caption: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "markdown": self.markdown,
            "rows": self.rows,
            "headers": self.headers,
            "num_rows": self.num_rows,
            "num_cols": self.num_cols,
            "bounding_box": asdict(self.bounding_box) if self.bounding_box else None,
            "caption": self.caption,
        }


@dataclass
class ParsedElement:
    """Represents a parsed element from a document"""
    type: ElementType
    content: str
    bounding_box: Optional[BoundingBox] = None
    metadata: Optional[Dict[str, Any]] = None
    table: Optional[ParsedTable] = None    # populated when type == TABLE


@dataclass
class PageContent:
    """Content extracted from a single page"""
    page_number: int
    text: str
    markdown: str
    elements: List[ParsedElement] = field(default_factory=list)
    tables: List[ParsedTable] = field(default_factory=list)
    has_tables: bool = False
    width: Optional[float] = None    # page width in points
    height: Optional[float] = None   # page height in points


def _bbox_from_prov(prov: Any, page_no: int) -> Optional[BoundingBox]:
    """Convert a Docling BoundingBox provenance to our BoundingBox dataclass."""
    try:
        b = prov.bbox
        return BoundingBox(
            x=b.l, y=b.t,
            width=b.r - b.l,
            height=b.b - b.t,
            page=page_no,
            coordinate_type="points",
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Fallback: plain regex-based parser (used when Docling is not available or
# the input is a LlamaIndex Document rather than a raw file path)
# ---------------------------------------------------------------------------

class _FallbackParser:
    """Lightweight regex parser used as a fallback."""

    @staticmethod
    def _element_type(line: str) -> ElementType:
        line = line.strip()
        if re.match(r"^#{1,6}\s+", line):
            return ElementType.HEADING
        if "|" in line or re.match(r"\+[-=]+\+", line):
            return ElementType.TABLE
        if line.startswith("```") or line.startswith("    "):
            return ElementType.CODE
        if re.match(r"^[\*\-\+]\s+", line) or re.match(r"^\d+\.\s+", line):
            return ElementType.LIST
        return ElementType.TEXT

    @staticmethod
    def _table_from_markdown(raw: str, page: int) -> ParsedTable:
        rows = [
            [c.strip() for c in r.strip("|").split("|")]
            for r in raw.strip().splitlines()
            if not re.match(r"^\|[-:\s|]+\|$", r.strip())
        ]
        headers = rows[0] if rows else []
        cells = [
            TableCell(row=ri, col=ci, text=cell,
                      is_header=(ri == 0))
            for ri, row in enumerate(rows)
            for ci, cell in enumerate(row)
        ]
        return ParsedTable(
            markdown=raw,
            rows=rows,
            cells=cells,
            headers=headers,
            num_rows=len(rows),
            num_cols=len(rows[0]) if rows else 0,
            bounding_box=BoundingBox(x=0, y=0, width=0, height=0, page=page),
        )

    def parse_page(self, text: str, page_num: int) -> PageContent:
        elements: List[ParsedElement] = []
        tables: List[ParsedTable] = []

        # --- extract markdown tables first, then remove them from text so
        #     table rows are not double-processed as plain-text elements ---
        table_pattern = r"(\|.*\|(?:\n\|[-:\s|]+\|(?:\n\|.*\|)*)*)"
        for m in re.finditer(table_pattern, text):
            t = self._table_from_markdown(m.group(1), page_num)
            tables.append(t)
        remaining = re.sub(table_pattern, "", text)

        # --- element extraction (table content already stripped from remaining) ---
        for idx, line in enumerate(remaining.splitlines()):
            if not line.strip():
                continue
            etype = self._element_type(line)
            elements.append(ParsedElement(
                type=etype,
                content=line.strip(),
                bounding_box=BoundingBox(x=0, y=float(idx), width=float(len(line)),
                                         height=1.0, page=page_num),
                metadata={"line_number": idx},
            ))

        md_lines = [e.content for e in elements]
        return PageContent(
            page_number=page_num,
            text=text,
            markdown="\n\n".join(md_lines),
            elements=elements,
            tables=tables,
            has_tables=len(tables) > 0,
        )


# ---------------------------------------------------------------------------
# Main Docling-powered parser
# ---------------------------------------------------------------------------

class DocumentParser:
    """
    State-of-the-art AI document parser backed by IBM Docling.

    Capabilities
    ------------
    * Layout-aware PDF parsing (multi-column, scanned via OCR, tables, figures)
    * Per-page markdown export
    * Structured table extraction with cell-level data + bounding boxes
    * Element-level bounding boxes (text blocks, headings, images, tables)
    * Graceful fallback to regex when Docling is unavailable

    Usage
    -----
    Quick (in-memory Documents):
        parser = DocumentParser()
        pages = parser.parse_document(llama_index_doc)

    Native file path (best quality for PDF):
        pages = parser.parse_file("report.pdf")
    """

    def __init__(self, use_ocr: bool = False, ocr_lang: str = "en", process_images: bool = False):
        """
        Parameters
        ----------
        use_ocr : bool
            Enable Docling's EasyOCR pipeline for scanned PDFs.
        ocr_lang : str
            Language hint for OCR (default "en").
        process_images : bool
            Enable image processing and scaling (default False for faster parsing).
        """
        self.use_ocr = use_ocr
        self.ocr_lang = ocr_lang
        self.process_images = process_images
        self._docling_pipeline = None
        self._converter = None
        self._fallback = _FallbackParser()
        self._init_docling()

    def _init_docling(self) -> None:
        """Try to initialise Docling pipeline; silently fall back on failure."""
        try:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.pipeline_options import (
                PdfPipelineOptions,
                TableFormerMode,
            )
            from docling.datamodel.base_models import InputFormat

            pipeline_options = PdfPipelineOptions()
            pipeline_options.do_ocr = self.use_ocr
            pipeline_options.do_table_structure = True
            # FAST mode uses ~4× less memory than ACCURATE; avoids std::bad_alloc
            # on large/high-resolution PDFs
            pipeline_options.table_structure_options.mode = TableFormerMode.FAST
            pipeline_options.table_structure_options.do_cell_matching = False
            # images_scale must be a non-zero float; 0.0 causes "Crop exceeds page dimensions"
            pipeline_options.images_scale = 0.5
            # Disable expensive image/figure generation when not needed
            pipeline_options.generate_page_images = self.process_images
            pipeline_options.generate_picture_images = self.process_images

            # ── GPU acceleration ──────────────────────────────────────────
            try:
                import torch
                from docling.datamodel.pipeline_options import (
                    AcceleratorOptions,
                    AcceleratorDevice,
                )
                if torch.cuda.is_available():
                    pipeline_options.accelerator_options = AcceleratorOptions(
                        num_threads=4,
                        device=AcceleratorDevice.CUDA,
                    )
                    logger.info("Docling: using CUDA GPU (%s)", torch.cuda.get_device_name(0))
                else:
                    logger.info("Docling: CUDA not available, using CPU")
            except (ImportError, Exception) as _gpu_exc:
                logger.debug("Docling AcceleratorOptions unavailable (%s), using default device", _gpu_exc)

            self._converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_options=pipeline_options
                    )
                }
            )
            logger.info("Docling AI parser initialised (OCR=%s, TableFormer=FAST, process_images=%s)", self.use_ocr, self.process_images)
        except Exception as exc:
            logger.warning(
                "Docling not available (%s). Using fallback regex parser.", exc
            )
            self._converter = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_file(self, file_path: str | Path) -> List[PageContent]:
        """
        Parse a PDF (or other Docling-supported format) directly from disk.

        This is the **highest quality** entry point — use it whenever you
        have a file path instead of a pre-loaded Document.

        Parameters
        ----------
        file_path : str | Path

        Returns
        -------
        List[PageContent], one item per page.
        """
        file_path = Path(file_path)
        if self._converter is None:
            # fallback: read raw text and parse
            text = file_path.read_text(encoding="utf-8", errors="replace")
            return [self._fallback.parse_page(text, 1)]

        try:
            result = self._converter.convert(str(file_path))
            return self._docling_result_to_pages(result)
        except Exception as exc:
            error_str = str(exc)
            
            # Quick-fail: Detect PDF dimension errors (corrupted metadata)
            # These cannot be fixed in Docling preprocessing, skip to fallback
            if "Crop exceeds page dimensions" in error_str:
                logger.warning(
                    "PDF has corrupted page dimensions. Docling preprocessing failed. "
                    "Using PyPDF2 fallback for %s", file_path
                )
                if str(file_path).lower().endswith('.pdf'):
                    try:
                        return self._extract_pdf_fallback(file_path)
                    except Exception as pdf_exc:
                        logger.warning("PyPDF2 fallback also failed: %s", pdf_exc)
            
            # Generic Docling error handling
            logger.warning("Docling conversion failed for %s: %s", file_path, exc)
            
            # Try PyPDF2 fallback for PDFs
            if str(file_path).lower().endswith('.pdf'):
                try:
                    return self._extract_pdf_fallback(file_path)
                except Exception as pdf_exc:
                    logger.warning("PyPDF2 fallback also failed: %s", pdf_exc)
            
            # Last resort: try reading as text
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
                return [self._fallback.parse_page(text, 1)]
            except Exception as text_exc:
                logger.error("All parsing methods failed for %s: %s", file_path, text_exc)
                # Return empty page rather than crashing
                return [PageContent(page_number=1, text="", markdown="")]

    def parse_document(self, document: Document) -> List[PageContent]:
        """
        Parse a LlamaIndex Document.

        If the document's metadata contains ``file_path`` and that file
        exists on disk, Docling is used for best quality. Otherwise the
        fallback regex parser is used.
        """
        file_path_str = (document.metadata or {}).get("file_path")
        if file_path_str and Path(file_path_str).exists() and self._converter:
            pages = self.parse_file(file_path_str)
            # honour specific page filter if caller gave a single-page doc
            target_page = (document.metadata or {}).get("page")
            if target_page is not None:
                pages = [p for p in pages if p.page_number == int(target_page)]
                if not pages:
                    pages = [self._fallback.parse_page(document.text, int(target_page))]
            return pages

        # fallback: parse the embedded text directly
        raw_pages = re.split(r"\n\s*\f\s*\n|\n---\n|\n\*\*\*\n", document.text)
        base_page = int((document.metadata or {}).get("page", 1))
        result = []
        for idx, page_text in enumerate(raw_pages):
            page_text = page_text.strip()
            if page_text:
                result.append(self._fallback.parse_page(page_text, base_page + idx))
        return result

    def parse_batch(
        self,
        documents: List[Document],
    ) -> Dict[str, List[PageContent]]:
        """Parse multiple LlamaIndex Documents."""
        results: Dict[str, List[PageContent]] = {}
        for doc in documents:
            doc_id = doc.doc_id or (doc.metadata or {}).get("file_name", "unknown")
            try:
                pages = self.parse_document(doc)
                results[doc_id] = pages
                logger.info("Parsed %s → %d pages", doc_id, len(pages))
            except Exception as exc:
                logger.error("Error parsing %s: %s", doc_id, exc)
        return results

    # ------------------------------------------------------------------
    # Docling conversion helpers
    # ------------------------------------------------------------------

    def _docling_result_to_pages(self, result: Any) -> List[PageContent]:
        """
        Convert a Docling ConversionResult into our PageContent list.
        """
        pages_map: Dict[int, PageContent] = {}

        # --- import Docling types individually so that a missing class in any
        # Docling version (e.g. ListItem / CodeItem absent in v2.x) does NOT
        # silently set TableItem = None and drop all table detection. ---
        try:
            from docling.datamodel.document import SectionHeaderItem
        except ImportError:
            SectionHeaderItem = None  # type: ignore
        try:
            from docling.datamodel.document import TableItem
        except ImportError:
            TableItem = None  # type: ignore
        try:
            from docling.datamodel.document import PictureItem
        except ImportError:
            PictureItem = None  # type: ignore
        try:
            from docling.datamodel.document import ListItem
        except ImportError:
            ListItem = None  # type: ignore
        try:
            from docling.datamodel.document import CodeItem
        except ImportError:
            CodeItem = None  # type: ignore

        doc = result.document
        # --- iterate all items in reading order ---
        for item, level in doc.iterate_items():
            prov = item.prov[0] if item.prov else None
            page_no = prov.page_no if prov else 1

            if page_no not in pages_map:
                pages_map[page_no] = PageContent(
                    page_number=page_no,
                    text="",
                    markdown="",
                )

            page = pages_map[page_no]
            bbox = _bbox_from_prov(prov, page_no) if prov else None

            if SectionHeaderItem and isinstance(item, SectionHeaderItem):
                # In docling 2.x level is a plain int; older versions used an
                # enum with a .value attribute.
                depth = level if isinstance(level, int) else getattr(level, "value", 1)
                prefix = "#" * max(1, depth)
                content = f"{prefix} {item.text}"
                etype = ElementType.HEADING

            elif TableItem and isinstance(item, TableItem):
                parsed_table = self._docling_table_to_parsed(item, page_no, bbox, doc)
                page.tables.append(parsed_table)
                page.has_tables = True
                elem = ParsedElement(
                    type=ElementType.TABLE,
                    content=parsed_table.markdown,
                    bounding_box=bbox,
                    metadata={"num_rows": parsed_table.num_rows,
                              "num_cols": parsed_table.num_cols},
                    table=parsed_table,
                )
                page.elements.append(elem)
                continue

            elif PictureItem and isinstance(item, PictureItem):
                try:
                    content = item.caption_text(doc) or "[Figure]"
                except (TypeError, AttributeError):
                    # docling 2.x may have removed / renamed caption_text
                    content = getattr(item, "text", None) or "[Figure]"
                etype = ElementType.FIGURE

            elif ListItem and isinstance(item, ListItem):
                content = f"- {item.text}"
                etype = ElementType.LIST

            elif CodeItem and isinstance(item, CodeItem):
                content = f"```\n{item.text}\n```"
                etype = ElementType.CODE

            else:
                content = getattr(item, "text", str(item))
                etype = ElementType.TEXT

            elem = ParsedElement(
                type=etype,
                content=content,
                bounding_box=bbox,
            )
            page.elements.append(elem)

        # --- build per-page markdown and text ---
        for page in pages_map.values():
            md_parts = []
            for elem in page.elements:
                md_parts.append(elem.content)
            page.markdown = "\n\n".join(md_parts)
            page.text = re.sub(r"```.*?```", "", page.markdown, flags=re.DOTALL)
            page.text = re.sub(r"#+ ", "", page.text).strip()

        # --- extract page dimensions from Docling ---
        try:
            for page_no, page_content in pages_map.items():
                doc_pages = getattr(doc, "pages", None)
                if doc_pages is not None:
                    page_info = doc_pages.get(page_no)
                    if page_info is not None:
                        size = getattr(page_info, "size", None)
                        if size is not None:
                            page_content.width = float(getattr(size, "width", 0) or 0) or None
                            page_content.height = float(getattr(size, "height", 0) or 0) or None
        except Exception as _dim_exc:
            logger.debug("Could not extract page dimensions from Docling: %s", _dim_exc)

        return [pages_map[k] for k in sorted(pages_map)]

    def _docling_table_to_parsed(
        self,
        table_item: Any,
        page_no: int,
        bbox: Optional[BoundingBox],
        doc: Any = None,
    ) -> ParsedTable:
        """Convert a Docling TableItem into our ParsedTable."""
        try:
            df = table_item.export_to_dataframe()
            headers = list(df.columns)
            rows: List[List[str]] = [[str(v) for v in row] for row in df.values.tolist()]
            all_rows = [headers] + rows

            # pass `doc` to avoid deprecation warning in newer Docling versions
            try:
                md = table_item.export_to_markdown(doc=doc) if doc is not None else table_item.export_to_markdown()
            except TypeError:
                md = table_item.export_to_markdown()

            cells: List[TableCell] = []
            for ri, row in enumerate(all_rows):
                for ci, cell_text in enumerate(row):
                    cells.append(TableCell(
                        row=ri, col=ci, text=cell_text,
                        is_header=(ri == 0),
                    ))

            return ParsedTable(
                markdown=md,
                rows=all_rows,
                cells=cells,
                headers=headers,
                num_rows=len(all_rows),
                num_cols=len(headers),
                bounding_box=bbox,
            )
        except Exception as exc:
            logger.warning("Could not convert Docling table: %s", exc)
            try:
                md = table_item.export_to_markdown()
            except Exception:
                md = "[table]"
            return ParsedTable(
                markdown=md, rows=[], cells=[], headers=[],
                num_rows=0, num_cols=0, bounding_box=bbox,
            )

    def _extract_pdf_fallback(self, file_path: Path) -> List[PageContent]:
        """
        Extract text from a PDF using PyPDF2 as fallback when Docling fails.
        
        This method attempts to recover what structure we can from a problematic
        PDF (e.g., with corrupted dimension metadata). While not as rich as
        Docling output, PyPDF2 can often extract text when Docling's preprocessing
        fails.
        
        Parameters
        ----------
        file_path : Path
            Path to the PDF file
            
        Returns
        -------
        List[PageContent]
            List of PageContent objects, one per page
        """
        try:
            import PyPDF2
        except ImportError:
            logger.error("PyPDF2 not available for PDF fallback extraction")
            raise ImportError("PyPDF2 required for PDF fallback")
        logger.info("Attempting PDF fallback extraction for %s using PyPDF2", file_path)
        pages_content: List[PageContent] = []
        
        try:
            with open(file_path, 'rb') as pdf_file:
                pdf_reader = PyPDF2.PdfReader(pdf_file)
                num_pages = len(pdf_reader.pages)
                logger.info("Extracting %d pages from %s using PyPDF2", num_pages, file_path.name)
                
                for page_num, page in enumerate(pdf_reader.pages, start=1):
                    try:
                        # Extract text from page
                        text = page.extract_text() or ""
                        
                        # Try to extract table information if available
                        page_content = self._fallback.parse_page(text, page_num)
                        pages_content.append(page_content)
                        logger.debug("Extracted page %d: %d characters", page_num, len(text))
                    except Exception as page_exc:
                        logger.warning("Failed to extract page %d: %s", page_num, page_exc)
                        # Add empty page rather than skip
                        pages_content.append(PageContent(
                            page_number=page_num,
                            text="",
                            markdown="",
                        ))
                
                logger.info("PyPDF2 extraction complete: %d pages extracted", len(pages_content))
                return pages_content
                
        except Exception as exc:
            logger.error("PyPDF2 extraction failed: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self, page_content: PageContent) -> Dict[str, Any]:
        return {
            "page_number": page_content.page_number,
            "text": page_content.text,
            "markdown": page_content.markdown,
            "elements": [
                {
                    "type": el.type.value,
                    "content": el.content,
                    "bounding_box": asdict(el.bounding_box) if el.bounding_box else None,
                    "metadata": el.metadata,
                }
                for el in page_content.elements
            ],
            "tables": [t.to_dict() for t in page_content.tables],
            "has_tables": page_content.has_tables,
        }


# ---------------------------------------------------------------------------
# HierarchicalParser – wraps DocumentParser, adds heading hierarchy tracking
# ---------------------------------------------------------------------------

class HierarchicalParser(DocumentParser):
    """
    Extends DocumentParser with document-level heading hierarchy tracking.

    After parsing, each element's metadata gains a 'hierarchy' dict:
        {"h1": "<title>", "h2": "<section>", "h3": "<subsection>"}
    This is used downstream by the indexer to build parent/child chunk links.
    """

    def __init__(self, enable_hierarchical: bool = True, process_images: bool = False, **kwargs):
        super().__init__(process_images=process_images, **kwargs)
        self.enable_hierarchical = enable_hierarchical

    def parse_document(self, document: Document) -> List[PageContent]:
        pages = super().parse_document(document)
        if self.enable_hierarchical:
            pages = self._annotate_hierarchy(pages)
        return pages

    def parse_file(self, file_path: str | Path) -> List[PageContent]:
        pages = super().parse_file(file_path)
        if self.enable_hierarchical:
            pages = self._annotate_hierarchy(pages)
        return pages

    def _annotate_hierarchy(self, pages: List[PageContent]) -> List[PageContent]:
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


# ---------------------------------------------------------------------------
# Example / quick test
# ---------------------------------------------------------------------------

# if __name__ == "__main__":
#     import sys
#     logging.basicConfig(level=logging.INFO)

#     if len(sys.argv) < 2:
#         print("Usage: python document_parser.py <path_to_pdf>")
#         sys.exit(1)

#     pdf_path = sys.argv[1]
#     parser = HierarchicalParser(enable_hierarchical=True, use_ocr=False)
#     pages = parser.parse_file(pdf_path)

#     print(f"\n{'='*60}")
#     print(f"Parsed {len(pages)} pages from '{pdf_path}'")
#     for pg in pages:
#         print(f"\n--- Page {pg.page_number} ({len(pg.elements)} elements,"
#               f" {len(pg.tables)} tables) ---")
#         print(pg.markdown[:500])
#         for tbl in pg.tables:
#             print(f"  [Table] {tbl.num_rows}×{tbl.num_cols}")
#             if tbl.bounding_box:
#                 bb = tbl.bounding_box
#                 print(f"  [BBox]  x={bb.x:.1f} y={bb.y:.1f}"
#                       f" w={bb.width:.1f} h={bb.height:.1f}")