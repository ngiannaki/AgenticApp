"""LangChain-native indexing pipeline (v2).

Parses with LangChain document loaders (see langchain_loaders.py), splits
with LangChain text splitters (structure-preserving for markdown-flavored
types, see langchain_structure.py), embeds with the DEV/PROD-selected
embedding model (backend.config.get_langchain_embedding), and stores the
chunks + embeddings in Postgres (backend/core/vector_store/pg_store.py,
schema `rag`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from backend.config import get_langchain_embedding, settings
from backend.core.vector_store.pg_store import ChunkRecord, PgVectorStore
from backend.mcp_servers.indexing.langchain_loaders import load_documents_v2
from backend.mcp_servers.indexing.langchain_structure import split_markdown_preserving_structure, stitch_cross_page_tables

logger = logging.getLogger(__name__)

# content types whose raw Documents are already markdown-flavored (headings +
# inline tables) and should go through the structure-preserving splitter;
# everything else (csv/xlsx row-docs, pptx slides) is already
# atomic/structured enough for a plain character split.
_MARKDOWN_STRUCTURED_TYPES = {"pdf", "docx", "txt"}

_MAX_SLIDE_TITLE_CHARS = 80


def context_prefix(document_name: str | None, metadata: dict) -> str:
    """The "which document / sheet / slide is this from" lines put in front
    of a chunk's text, so the embedding and the keyword index know it (the
    "Section:" line for pdf/docx/txt is added earlier, by the splitter).
    No LLM involved: it is all read from the document's own structure."""
    lines = []
    if document_name and document_name != "Untitled":
        lines.append(f"Document: {document_name}")
    if metadata.get("sheet"):
        lines.append(f"Sheet: {metadata['sheet']}")
    if metadata.get("slide") is not None:
        title = (metadata.get("slide_title") or "").strip()[:_MAX_SLIDE_TITLE_CHARS]
        lines.append(f"Slide {metadata['slide']}" + (f": {title}" if title else ""))
    return "\n".join(lines) + "\n" if lines else ""


class LangchainIndexPipelineV2:
    """Parse -> split -> embed -> store, entirely on LangChain primitives."""

    def __init__(self, store: PgVectorStore | None = None):
        self.cfg = settings.rag_v2
        self.store = store or PgVectorStore()
        self.embedding_model = settings.embedding_model
        self.embeddings = get_langchain_embedding(self.embedding_model)
        logger.info("LangchainIndexPipelineV2 initialized (embedding model %s)", self.embedding_model)

    def _splitter(self) -> RecursiveCharacterTextSplitter:
        return RecursiveCharacterTextSplitter(
            chunk_size=self.cfg.chunk_size,
            chunk_overlap=self.cfg.chunk_overlap,
            add_start_index=True,
        )

    def _split(self, raw_docs: list[Document], content_type: str) -> tuple[list[Document], list[dict]]:
        """Split `raw_docs` into final chunks. Markdown-flavored types
        (pdf/docx/txt) go through the structure-preserving splitter (table-
        atomic chunks, header-aware section metadata); everything else uses
        the plain character splitter since those Documents are already
        atomic/structured (csv/xlsx rows, pptx slides).

        Returns (chunks, toc_entries) — toc_entries is the deduped, ordered
        list of headings seen across `raw_docs`, for the collection's TOC.
        """
        if content_type not in _MARKDOWN_STRUCTURED_TYPES:
            return self._splitter().split_documents(raw_docs), []

        split_docs: list[Document] = []
        toc_entries: list[dict] = []
        seen = set()
        # One section state for the whole document, so a page with no heading
        # of its own still carries the section it continues.
        heading_state: dict = {"h1": None, "h2": None, "h3": None}
        for doc in stitch_cross_page_tables(raw_docs):
            chunks, headings = split_markdown_preserving_structure(
                doc, self.cfg.chunk_size, self.cfg.chunk_overlap, heading_state
            )
            split_docs.extend(chunks)
            for h in headings:
                key = (h["level"], h["title"], h.get("page"))
                if key not in seen:
                    seen.add(key)
                    toc_entries.append(h)
        return split_docs, toc_entries

    def index_document(
        self,
        document_id: str,
        file_path: str | Path,
        content_type: str,
        document_name: str = "Untitled",
        user_id: str | None = None,
        extra_metadata: dict | None = None,
    ) -> dict:
        """Parse `file_path` (of `content_type`), split it, embed it, and
        store it as `document_id` (replacing any earlier index of it).
        Returns {success, document_id, chunks_indexed, ...}."""
        if not user_id:
            # Retrieval only ever serves a document to its owner, so an
            # ownerless index would be unreadable — refuse it up front.
            return {"success": False, "document_id": document_id, "error": "user_id is required to index a document"}
        try:
            logger.info(f"[INDEX_V2] Starting for document_id={document_id} type={content_type}")

            raw_docs = load_documents_v2(Path(file_path), content_type)
            if not raw_docs:
                return {"success": False, "document_id": document_id, "error": "No content extracted from document"}

            split_docs, toc_entries = self._split(raw_docs, content_type)
            if not split_docs:
                return {"success": False, "document_id": document_id, "error": "No chunks produced after splitting"}

            base_metadata = {
                "document_id": document_id,
                "document_name": document_name,
                "content_type": content_type,
                "pipeline": "langchain_v2",
                "user_id": user_id,
                **(extra_metadata or {}),
            }

            for i, doc in enumerate(split_docs):
                prefix = context_prefix(document_name, doc.metadata)
                if prefix:
                    doc.page_content = prefix + doc.page_content
                # Loader/splitter metadata (page, sheet, row, start_index, ...) is
                # grounding info worth keeping, but its own "source" is the
                # server-local file path store.create_document wrote to (an
                # internal detail, not something to cite) — rename it out of the
                # way so base_metadata's "source" (the original filename, what a
                # citation should show) isn't clobbered by a setdefault no-op.
                loader_metadata = dict(doc.metadata)
                loader_path = loader_metadata.pop("source", None)
                if loader_path:
                    loader_metadata["source_path"] = loader_path
                doc.metadata = {**loader_metadata, **base_metadata, "source": document_name, "chunk_index": i}
                doc.metadata["chunk_id"] = f"{document_id}-v2-{i}-{uuid4().hex[:8]}"

            vectors = self.embeddings.embed_documents([d.page_content for d in split_docs])
            self.store.replace_document(
                document_id=document_id,
                user_id=user_id,
                document_name=document_name,
                content_type=content_type,
                embedding_model=self.embedding_model,
                toc=toc_entries or None,
                chunks=[
                    ChunkRecord(
                        chunk_id=d.metadata["chunk_id"],
                        chunk_index=d.metadata["chunk_index"],
                        content=d.page_content,
                        metadata=d.metadata,
                        embedding=vec,
                    )
                    for d, vec in zip(split_docs, vectors)
                ],
            )

            logger.info(f"[INDEX_V2] Indexed {len(split_docs)} chunks for document_id={document_id}")

            return {
                "success": True,
                "document_id": document_id,
                "chunks_indexed": len(split_docs),
                "pipeline": "langchain_v2",
                "message": f"Document {document_id} indexed successfully (v2)",
            }
        except Exception as e:
            logger.error(f"[INDEX_V2] Failed for document_id={document_id}: {e}", exc_info=True)
            return {"success": False, "document_id": document_id, "error": str(e)}

    def delete_document(self, document_id: str) -> dict:
        """Delete `document_id`'s chunks and embeddings. Safe to call for a
        document that was never indexed or is already gone."""
        try:
            deleted = self.store.delete_document(document_id)
            logger.info(f"[INDEX_V2] Deleted index for {document_id} (existed={deleted})")
            return {"success": True, "document_id": document_id}
        except Exception as e:
            logger.warning(f"[INDEX_V2] Failed to delete index for {document_id}: {e}")
            return {"success": False, "document_id": document_id, "error": str(e)}
