"""LangChain-native indexing pipeline (v2).

Parallel to backend/infrastructure/vector_store/vector_store.py's
llama_index-based pipeline — not a replacement. Parses with LangChain
document loaders (see langchain_loaders.py), splits with LangChain text
splitters, embeds with the same DEV/PROD-selected embedding model as v1
(backend.config.get_langchain_embedding), and writes into the same Chroma
server but its own collections (`docv2_{document_id}`), so v1 and v2 data
never mix.
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

import chromadb
from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter

from backend.config import get_langchain_embedding, settings
from backend.mcp_servers.indexing.langchain_loaders import load_documents_v2

logger = logging.getLogger(__name__)


class LangchainIndexPipelineV2:
    """Parse -> split -> embed -> store, entirely on LangChain primitives."""

    def __init__(self, host: str | None = None, port: int | None = None):
        self.cfg = settings.rag_v2
        self.chroma_client = chromadb.HttpClient(
            host=host or settings.vector_store.host,
            port=port or settings.vector_store.port,
        )
        self.embeddings = get_langchain_embedding(settings.embedding_model)
        logger.info("LangchainIndexPipelineV2 initialized")

    def _collection_name(self, document_id: str) -> str:
        return f"{self.cfg.collection_prefix}{document_id}"

    def _splitter(self) -> RecursiveCharacterTextSplitter:
        return RecursiveCharacterTextSplitter(
            chunk_size=self.cfg.chunk_size,
            chunk_overlap=self.cfg.chunk_overlap,
            add_start_index=True,
        )

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
        write it into `docv2_{document_id}`. Returns the same
        {success, document_id, chunks_indexed, ...} shape as v1's
        index_document_enhanced so callers can treat both uniformly."""
        try:
            logger.info(f"[INDEX_V2] Starting for document_id={document_id} type={content_type}")

            raw_docs = load_documents_v2(Path(file_path), content_type)
            if not raw_docs:
                return {"success": False, "document_id": document_id, "error": "No content extracted from document"}

            split_docs = self._splitter().split_documents(raw_docs)
            if not split_docs:
                return {"success": False, "document_id": document_id, "error": "No chunks produced after splitting"}

            base_metadata = {
                "document_id": document_id,
                "document_name": document_name,
                "content_type": content_type,
                "pipeline": "langchain_v2",
                **(extra_metadata or {}),
            }
            if user_id:
                base_metadata["user_id"] = user_id

            ids = []
            for i, doc in enumerate(split_docs):
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
                chunk_id = f"{document_id}-v2-{i}-{uuid4().hex[:8]}"
                doc.metadata["chunk_id"] = chunk_id
                ids.append(chunk_id)

            collection_metadata = {"hnsw:space": "cosine", "document_id": document_id, "pipeline": "langchain_v2"}
            if user_id:
                collection_metadata["user_id"] = user_id

            collection_name = self._collection_name(document_id)
            vectorstore = Chroma(
                client=self.chroma_client,
                collection_name=collection_name,
                embedding_function=self.embeddings,
                collection_metadata=collection_metadata,
            )
            vectorstore.add_documents(split_docs, ids=ids)

            logger.info(f"[INDEX_V2] Indexed {len(split_docs)} chunks for document_id={document_id} into {collection_name}")

            return {
                "success": True,
                "document_id": document_id,
                "chunks_indexed": len(split_docs),
                "collection_name": collection_name,
                "pipeline": "langchain_v2",
                "message": f"Document {document_id} indexed successfully (v2)",
            }
        except Exception as e:
            logger.error(f"[INDEX_V2] Failed for document_id={document_id}: {e}", exc_info=True)
            return {"success": False, "document_id": document_id, "error": str(e)}

    def delete_document(self, document_id: str) -> dict:
        """Delete the v2 collection for `document_id`. Safe to call even if
        it was never indexed with v2, or already deleted. Does not touch the
        v1 (`doc_{document_id}`) collection."""
        collection_name = self._collection_name(document_id)
        try:
            self.chroma_client.delete_collection(name=collection_name)
            logger.info(f"[INDEX_V2] Deleted collection {collection_name}")
            return {"success": True, "document_id": document_id}
        except Exception as e:
            logger.warning(f"[INDEX_V2] Failed to delete collection {collection_name}: {e}")
            return {"success": False, "document_id": document_id, "error": str(e)}
