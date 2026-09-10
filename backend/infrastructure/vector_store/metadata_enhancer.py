"""
Enhanced metadata extraction for documents using LlamaIndex.

Provides:
- Document summaries
- Section/hierarchy extraction
- Document statistics
- Metadata enrichment
"""

import re
from typing import Optional, Dict, List
from llama_index.core import Document
from llama_index.core.node_parser import HierarchicalNodeParser, SimpleNodeParser
import logging

from backend.config import get_llamaindex_llm, settings

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)


def split_into_pages(text: str) -> List[Dict]:
    """Split markdown-flavored text into pseudo-"pages" at `#`-heading
    boundaries, so txt/md/docx content gets hierarchical-chunked per section
    instead of as one undifferentiated blob (which loses structure the same
    way one giant PDF page would). Content with no headings — a plain .log
    or .txt file — comes back as a single page, matching prior behavior.
    """

    if not text or not text.strip():
        return []

    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [{"page_number": 1, "text": text}]

    pages = []
    starts = [m.start() for m in matches] + [len(text)]
    # Anything before the first heading (e.g. an unlabeled intro paragraph)
    # becomes its own leading page instead of being dropped.
    if starts[0] > 0 and text[: starts[0]].strip():
        pages.append({"page_number": 1, "text": text[: starts[0]]})

    for i, start in enumerate(starts[:-1]):
        section = text[start : starts[i + 1]]
        if section.strip():
            pages.append({"page_number": len(pages) + 1, "text": section})

    return pages


class MetadataEnhancer:
    """Enhances document metadata with summaries, sections, and hierarchy info."""

    def __init__(self) -> None:
        """Initialize metadata enhancer with the configured LLM (Ollama in DEV, OpenAI in PROD)."""
        self.llm = get_llamaindex_llm(settings.metadata_model, temperature=settings.metadata_temperature)

        logger.info("MetadataEnhancer initialized with model=%s", settings.metadata_model)
    
    def extract_sections(self, text: str, top_k: int = 5) -> List[Dict]:
        """
        Extract main sections/topics from document text using LLM.
        
        Args:
            text: Document text
            top_k: Number of top sections to extract
        
        Returns:
            List of sections with titles and descriptions
        """
        try:
            if not text or len(text.strip()) < 100:
                return []
            
            prompt = f"""Extract the {top_k} main sections or topics from this document text.
Return as JSON array with "title" and "description" fields.

Text:
{text[:2000]}...

Return ONLY the JSON array, nothing else."""
            
            response = self.llm.complete(prompt)
            
            # Try to parse JSON from response
            try:
                import json
                # Find JSON array in response
                response_text = str(response)
                start_idx = response_text.find('[')
                end_idx = response_text.rfind(']') + 1
                
                if start_idx != -1 and end_idx > start_idx:
                    sections = json.loads(response_text[start_idx:end_idx])
                    return sections[:top_k]
            except:
                pass
            
            return []
        
        except Exception as e:
            logger.warning(f"Failed to extract sections: {str(e)}")
            return []
    
    def generate_summary(self, text: str, max_length: int = 500) -> str:
        """
        Generate a concise summary of the document.
        
        Args:
            text: Document text
            max_length: Maximum summary length
        
        Returns:
            Document summary
        """
        try:
            if not text or len(text.strip()) < 100:
                return ""
            
            prompt = f"""Summarize this document in {max_length} characters or less. 
Be concise and capture the main points.

Document:
{text[:3000]}...

Summary:"""
            
            response = self.llm.complete(prompt, max_tokens=max_length // 2)  # Rough estimate of tokens
            summary = str(response).strip()
            
            # Trim to max length
            # if len(summary) > max_length:
            #     summary = summary[:max_length] + "..."
            
            return summary
        
        except Exception as e:
            logger.warning(f"Failed to generate summary: {str(e)}")
            return ""
    
    def extract_metadata(self, text: str, document_name: str = "Untitled", content_type: str = "pdf") -> Dict:
        """
        Extract comprehensive metadata from document.

        Args:
            text: Document text
            document_name: Original document file name
            content_type: Detected type (pdf/csv/xlsx/txt/docx/pptx)

        Returns:
            Dictionary with enhanced metadata
        """
        try:
            word_count = len(text.split())
            char_count = len(text)

            metadata = {
                "document_name": document_name,
                "word_count": word_count,
                "char_count": char_count,
                "document_type": content_type,
            }
            
            # Generate summary
            if word_count > 50:
                summary = self.generate_summary(text)
                if summary:
                    metadata["summary"] = summary
            
            # Extract sections
            if word_count > 100:
                sections = self.extract_sections(text)
                if sections:
                    metadata["sections"] = sections
                    metadata["section_count"] = len(sections)
            
            logger.info(f"Enhanced metadata extracted: {len(metadata)} fields")
            return metadata
        
        except Exception as e:
            logger.error(f"Failed to extract metadata: {str(e)}")
            return {
                "document_name": document_name,
                "document_type": content_type,
                "error": str(e)
            }


class HierarchicalChunker:
    """Create hierarchical chunks using LlamaIndex HierarchicalNodeParser."""

    def __init__(self):
        """Initialize hierarchical node parser with production-optimized settings."""
        self.parser = HierarchicalNodeParser.from_defaults(
            chunk_sizes=[1024, 256],  # Large, medium, small chunks (optimized for ~400, ~100, ~25 tokens)
            chunk_overlap=100,  # ~47% overlap on smallest chunk for context continuity (must be < min chunk_size)
        )

        logger.info("HierarchicalChunker initialized with production chunk sizes: [1024, 256], overlap: 100")
    
    def create_hierarchical_chunks(
        self,
        text: str,
        document_id: str,
        document_name: str = "Untitled",
        page_number: int = 1,
        extra_metadata: Optional[Dict] = None,
    ) -> List[Dict]:
        """
        Create hierarchical chunks from text.

        Each chunk has information about its hierarchy level and parent chunks.

        Args:
            text: Text to chunk
            document_id: Document UUID
            document_name: Original document name
            page_number: Page number (for multi-page docs)
            extra_metadata: Additional fields merged into every chunk's metadata
                (e.g. user_id, content_type, slide_number, heading)

        Returns:
            List of chunks with hierarchy metadata
        """
        extra_metadata = extra_metadata or {}
        try:
            # Create Document object
            doc = Document(
                text=text,
                metadata={
                    "document_id": document_id,
                    "document_name": document_name,
                    "page_number": page_number,
                    **extra_metadata,
                }
            )

            # Parse into hierarchical nodes
            nodes = self.parser.get_nodes_from_documents([doc])

            # Convert nodes to chunk dictionaries
            chunks = []
            for i, node in enumerate(nodes):
                chunk = {
                    "text": node.get_content(),
                    "metadata": {
                        "document_id": document_id,
                        "document_name": document_name,
                        "page_number": page_number,
                        "chunk_index": i,
                        "chunk_id": node.node_id if hasattr(node, 'node_id') else f"{document_id}-chunk-{i}",
                        "hierarchy_level": getattr(node, 'level', 0) if hasattr(node, 'level') else 0,
                        "source": "hierarchical_parse",
                        **extra_metadata,
                    }
                }

                # Add parent node reference if exists
                if hasattr(node, 'parent_node') and node.parent_node:
                    chunk["metadata"]["parent_chunk_id"] = node.parent_node.node_id

                chunks.append(chunk)

            logger.info(f"Created {len(chunks)} hierarchical chunks for {document_id}")
            return chunks

        except Exception as e:
            logger.warning(f"Hierarchical chunking failed, falling back to simple chunks: {str(e)}")
            return [
                {
                    "text": text,
                    "metadata": {
                        "document_id": document_id,
                        "document_name": document_name,
                        "page_number": page_number,
                        "chunk_index": 0,
                        "source": "fallback_simple",
                        **extra_metadata,
                    }
                }
            ]

    def create_chunks_from_pages(
        self,
        pages: List[Dict],
        document_id: str,
        document_name: str = "Untitled",
        extra_metadata: Optional[Dict] = None,
    ) -> List[Dict]:
        """
        Create hierarchical chunks from pages.

        Args:
            pages: List of page dicts with 'text' and 'page_number' (may also
                carry type-specific fields like 'slide_number'/'title', which
                are merged into every chunk produced from that page)
            document_id: Document UUID
            document_name: Original document name
            extra_metadata: Additional fields merged into every chunk's metadata

        Returns:
            List of hierarchical chunks
        """
        all_chunks = []
        base_extra = extra_metadata or {}

        for page in pages:
            page_text = page.get("text", "")
            page_num = page.get("page_number", 1)
            page_extra = {
                **base_extra,
                **{k: v for k, v in page.items() if k not in ("text", "page_number") and v not in (None, "")},
            }

            if page_text.strip():
                page_chunks = self.create_hierarchical_chunks(
                    text=page_text,
                    document_id=document_id,
                    document_name=document_name,
                    page_number=page_num,
                    extra_metadata=page_extra,
                )
                all_chunks.extend(page_chunks)

        return all_chunks


class TabularChunker:
    """Chunk csv/xlsx content by row group rather than by character window.

    A plain sliding-window chunker over a rendered table risks cutting a row
    in half or separating data rows from their header, both of which make a
    retrieved chunk useless (or misleading) on its own. Instead each chunk is
    a batch of whole rows with the header repeated at the top, plus one
    summary chunk per sheet so the agent can answer "what's in this file"
    without having retrieved every row.
    """

    def __init__(self, rows_per_chunk: int = 50):
        self.rows_per_chunk = rows_per_chunk

    def create_chunks_from_sheets(
        self,
        sheets: List[Dict],
        document_id: str,
        document_name: str = "Untitled",
        extra_metadata: Optional[Dict] = None,
    ) -> List[Dict]:
        """
        Args:
            sheets: list of {"name": str, "headers": list[str], "rows": list[list]}
                (a single-sheet CSV can pass one entry with name="Sheet1")
        """
        base_extra = extra_metadata or {}
        chunks: List[Dict] = []

        for sheet in sheets:
            sheet_name = sheet.get("name", "Sheet1")
            headers = [str(h) for h in sheet.get("headers", [])]
            rows = sheet.get("rows", [])

            chunks.append(self._summary_chunk(sheet_name, headers, rows, document_id, document_name, base_extra))

            for start in range(0, len(rows), self.rows_per_chunk):
                batch = rows[start : start + self.rows_per_chunk]
                end = start + len(batch) - 1
                text = self._render_table(headers, batch)
                chunks.append(
                    {
                        "text": text,
                        "metadata": {
                            "document_id": document_id,
                            "document_name": document_name,
                            "sheet_name": sheet_name,
                            "row_start": start,
                            "row_end": end,
                            "source": "tabular_rows",
                            **base_extra,
                        },
                    }
                )

        return chunks

    def _summary_chunk(
        self,
        sheet_name: str,
        headers: List[str],
        rows: List[list],
        document_id: str,
        document_name: str,
        base_extra: Dict,
    ) -> Dict:
        columns = ", ".join(headers) if headers else "(no header row)"
        text = f"Table '{sheet_name}' has {len(rows)} rows and columns: {columns}"
        return {
            "text": text,
            "metadata": {
                "document_id": document_id,
                "document_name": document_name,
                "sheet_name": sheet_name,
                "row_count": len(rows),
                "source": "tabular_summary",
                **base_extra,
            },
        }

    @staticmethod
    def _render_table(headers: List[str], rows: List[list]) -> str:
        cell = lambda v: "" if v is None else str(v)
        lines = []
        if headers:
            lines.append(" | ".join(cell(h) for h in headers))
            lines.append(" | ".join("---" for _ in headers))
        lines.extend(" | ".join(cell(c) for c in row) for row in rows)
        return "\n".join(lines)
