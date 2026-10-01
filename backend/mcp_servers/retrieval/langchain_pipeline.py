"""LangChain-native hybrid retrieval pipeline (v2).

Parallel to retriever.py's llama_index-based DocumentRetriever — not a
replacement. Reads the `docv2_{document_id}` collections written by
backend/mcp_servers/indexing/langchain_pipeline.py and combines two
LangChain retrievers via reciprocal-rank fusion:

- dense: a Chroma similarity retriever (backend.config.get_langchain_embedding,
  Ollama in DEV / OpenAI in PROD — same model as v1, so vectors stay
  comparable across pipelines even though collections are separate)
- sparse: a BM25 keyword retriever, built on the fly from that same
  collection's chunks

`langchain.retrievers.EnsembleRetriever` fuses the two, and (when enabled)
`ContextualCompressionRetriever` + a HuggingFace cross-encoder reranks the
fused candidates down to `top_k` — the current state-of-the-art recipe for
small-corpus RAG: dense retrieval alone misses exact keyword/ID matches that
BM25 catches, and reranking corrects ordering mistakes either retriever
makes alone.

Every returned chunk carries a `grounding` block (document_name, source,
page/sheet/row when the original loader captured one, chunk_id, retrieval
signals) so a caller can cite exactly where an answer came from.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

import chromadb
from langchain_chroma import Chroma
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document

from backend.config import get_langchain_embedding, settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=2)
def _get_cross_encoder(model_name: str):
    """Cached so the (heavyweight) HuggingFace cross-encoder model is loaded
    once per process, not once per query."""
    from langchain_community.cross_encoders import HuggingFaceCrossEncoder

    logger.info(f"[RETRIEVAL_V2] Loading cross-encoder {model_name}")
    return HuggingFaceCrossEncoder(model_name=model_name)


def _build_reranker(model_name: str, top_n: int):
    from langchain.retrievers.document_compressors import CrossEncoderReranker

    return CrossEncoderReranker(model=_get_cross_encoder(model_name), top_n=top_n)


class LangchainRetrievalPipelineV2:
    """Hybrid (dense + BM25) retrieval with cross-encoder reranking, over
    the `docv2_*` collections."""

    def __init__(self, host: str | None = None, port: int | None = None):
        self.cfg = settings.rag_v2
        self.chroma_client = chromadb.HttpClient(
            host=host or settings.vector_store.host,
            port=port or settings.vector_store.port,
        )
        self.embeddings = get_langchain_embedding(settings.embedding_model)
        logger.info("LangchainRetrievalPipelineV2 initialized")

    def _collection_name(self, document_id: str) -> str:
        return f"{self.cfg.collection_prefix}{document_id}"

    def _check_ownership(self, collection, user_id: str | None) -> bool:
        """Only the collection's owner may read it. A collection with no
        recorded owner is denied too (it used to be treated as shared): every
        document uploaded through the API is stamped with its user_id, so an
        ownerless collection can only come from a legacy/out-of-band upload,
        and "readable by everyone" is the wrong default for that in a
        multi-user deployment."""
        owner = (collection.metadata or {}).get("user_id")
        if not owner or owner != user_id:
            logger.warning(f"[RETRIEVAL_V2] Denied access to {collection.name} for user_id={user_id!r} (owned by {owner!r})")
            return False
        return True

    def _load_all_documents(self, collection) -> list[Document]:
        result = collection.get(include=["documents", "metadatas"])
        texts = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        ids = result.get("ids") or []
        docs = []
        for text, metadata, chunk_id in zip(texts, metadatas, ids):
            meta = dict(metadata or {})
            meta.setdefault("chunk_id", chunk_id)
            docs.append(Document(page_content=text, metadata=meta))
        return docs

    @staticmethod
    def _build_where(filters: dict | None) -> dict | None:
        """Turn a flat {field: value} filter dict into a Chroma `where`
        clause. Exact-match only — callers filter on metadata fields written
        at index time (chunk_content_type, page, sheet, h1/h2/h3, ...)."""
        if not filters:
            return None
        clauses = [{k: {"$eq": v}} for k, v in filters.items() if v is not None]
        if not clauses:
            return None
        return clauses[0] if len(clauses) == 1 else {"$and": clauses}

    @staticmethod
    def _matches_filters(metadata: dict, filters: dict | None) -> bool:
        if not filters:
            return True
        return all(metadata.get(k) == v for k, v in filters.items() if v is not None)

    def _build_retriever(
        self,
        document_id: str,
        collection,
        all_docs: list[Document],
        fetch_k: int,
        where: dict | None,
        filters: dict | None,
    ):
        vectorstore = Chroma(
            client=self.chroma_client,
            collection_name=self._collection_name(document_id),
            embedding_function=self.embeddings,
        )
        search_kwargs: dict = {"k": fetch_k}
        if where:
            search_kwargs["filter"] = where
        dense = vectorstore.as_retriever(search_kwargs=search_kwargs)

        filtered_docs = [d for d in all_docs if self._matches_filters(d.metadata, filters)]
        if not filtered_docs:
            return dense, False

        bm25 = BM25Retriever.from_documents(filtered_docs)
        bm25.k = fetch_k

        from langchain.retrievers import EnsembleRetriever

        ensemble = EnsembleRetriever(
            retrievers=[dense, bm25],
            weights=[self.cfg.dense_weight, self.cfg.sparse_weight],
        )
        return ensemble, True

    @staticmethod
    def _section(metadata: dict) -> str | None:
        parts = [metadata.get(k) for k in ("h1", "h2", "h3") if metadata.get(k)]
        return " > ".join(parts) if parts else None

    @classmethod
    def _grounding(cls, metadata: dict, expanded: bool = False) -> dict:
        """The subset of a chunk's metadata that identifies where it came
        from, in a stable shape regardless of source format."""
        location = (
            metadata.get("page")
            if metadata.get("page") is not None
            else metadata.get("slide")
            if metadata.get("slide") is not None
            else metadata.get("sheet")
            if metadata.get("sheet") is not None
            else metadata.get("row")
        )
        return {
            "document_id": metadata.get("document_id"),
            "document_name": metadata.get("document_name"),
            "source": metadata.get("source"),
            "location": location,
            "chunk_id": metadata.get("chunk_id"),
            "chunk_index": metadata.get("chunk_index"),
            "content_type": metadata.get("content_type"),
            "chunk_content_type": metadata.get("chunk_content_type"),
            "section": cls._section(metadata),
            "expanded": expanded,
        }

    def _expand_neighbors(self, docs: list[Document], all_docs: list[Document]) -> list[tuple[Document, bool]]:
        """For each hit, splice in ±neighbor_window adjacent chunks (by
        chunk_index within the same source) so a citation carries more
        surrounding context than one isolated chunk. Returns (doc, expanded)
        pairs — `doc.page_content` is replaced with the merged text when
        expansion happened; the original chunk's own metadata is kept."""
        window = self.cfg.neighbor_window
        if not window:
            return [(d, False) for d in docs]

        by_key: dict[tuple, Document] = {}
        for d in all_docs:
            idx = d.metadata.get("chunk_index")
            if idx is not None:
                by_key[(d.metadata.get("source"), idx)] = d

        out = []
        for doc in docs:
            idx = doc.metadata.get("chunk_index")
            source = doc.metadata.get("source")
            if idx is None:
                out.append((doc, False))
                continue

            pieces = []
            expanded = False
            for offset in range(-window, window + 1):
                neighbor = by_key.get((source, idx + offset))
                if neighbor is None:
                    continue
                if offset != 0:
                    expanded = True
                pieces.append(neighbor.page_content)

            if expanded:
                merged = Document(page_content="\n\n".join(pieces), metadata=doc.metadata)
                out.append((merged, True))
            else:
                out.append((doc, False))
        return out

    def retrieve(
        self,
        document_id: str,
        query: str,
        top_k: int | None = None,
        user_id: str | None = None,
        filters: dict | None = None,
    ) -> dict:
        """Hybrid-retrieve + rerank chunks for `query` from `docv2_{document_id}`.

        Does not synthesize an answer — same contract as v1's retrieve():
        returns source chunks only, each with `text`, `score`
        (reranker relevance score when reranking ran, else the vector
        similarity score), `metadata`, and a `grounding` block.

        `filters` restricts candidates to exact metadata matches before
        ranking — e.g. {"chunk_content_type": "table"} to only search table
        chunks, {"page": 3} for one page, {"h1": "..."} for one section.
        Applied identically to the dense (Chroma `where`) and BM25 legs.
        """
        top_k = min(top_k or self.cfg.default_top_k, self.cfg.max_top_k)
        fetch_k = min(max(top_k * self.cfg.fetch_k_multiplier, 10), self.cfg.max_fetch_k)
        where = self._build_where(filters)

        if not query or not query.strip():
            return {"success": False, "document_id": document_id, "error": "Query parameter is required and cannot be empty"}

        collection_name = self._collection_name(document_id)
        try:
            collection = self.chroma_client.get_collection(name=collection_name)
        except Exception:
            return {
                "success": False,
                "document_id": document_id,
                "query": query,
                "error": f"Document not found in v2 vector store: {document_id}",
            }

        if not self._check_ownership(collection, user_id):
            return {
                "success": False,
                "document_id": document_id,
                "query": query,
                "error": f"Document not found in v2 vector store: {document_id}",
            }

        try:
            all_docs = self._load_all_documents(collection)
            retriever, used_bm25 = self._build_retriever(document_id, collection, all_docs, fetch_k, where, filters)

            reranked = False
            if self.cfg.use_reranker and all_docs:
                try:
                    from langchain.retrievers import ContextualCompressionRetriever

                    compressor = _build_reranker(self.cfg.rerank_model, top_n=top_k)
                    retriever = ContextualCompressionRetriever(base_compressor=compressor, base_retriever=retriever)
                    reranked = True
                except Exception as e:
                    logger.warning(f"[RETRIEVAL_V2] Reranker unavailable, skipping: {e}")

            docs = retriever.invoke(query)
            docs = docs[:top_k]

            expanded_docs = self._expand_neighbors(docs, all_docs) if self.cfg.neighbor_expansion else [(d, False) for d in docs]

            source_chunks = []
            for doc, expanded in expanded_docs:
                metadata = dict(doc.metadata)
                score = metadata.pop("relevance_score", None)
                source_chunks.append(
                    {
                        "text": doc.page_content,
                        "score": score,
                        "metadata": metadata,
                        "grounding": self._grounding(metadata, expanded=expanded),
                    }
                )

            logger.info(
                f"[RETRIEVAL_V2] document_id={document_id} hybrid={used_bm25} reranked={reranked} -> {len(source_chunks)} chunks"
            )

            return {
                "success": True,
                "document_id": document_id,
                "query": query,
                "source_chunks": source_chunks,
                "chunks_count": len(source_chunks),
                "pipeline": "langchain_v2",
                "retrieval_mode": "hybrid" if used_bm25 else "dense",
                "reranked": reranked,
                "filters": filters,
            }
        except Exception as e:
            logger.exception(f"[RETRIEVAL_V2] Retrieval failed for document_id={document_id}")
            return {"success": False, "document_id": document_id, "query": query, "error": str(e)}

    def get_document_info(self, document_id: str, user_id: str | None = None) -> dict:
        """Metadata/statistics for a document's v2 collection — same shape
        family as v1's get_document_metadata."""
        collection_name = self._collection_name(document_id)
        try:
            collection = self.chroma_client.get_collection(name=collection_name)
        except Exception:
            return {"success": False, "document_id": document_id, "error": f"Document not found in v2 vector store: {document_id}"}

        if not self._check_ownership(collection, user_id):
            return {"success": False, "document_id": document_id, "error": f"Document not found in v2 vector store: {document_id}"}

        try:
            result = collection.get(limit=1000, include=["metadatas"])
            all_metadata = result.get("metadatas") or []

            info: dict = {
                "success": True,
                "document_id": document_id,
                "collection_metadata": collection.metadata or {},
                "total_chunks": len(all_metadata),
                "pipeline": "langchain_v2",
            }
            if all_metadata:
                info["document_name"] = all_metadata[0].get("document_name", "Untitled")
                info["content_type"] = all_metadata[0].get("content_type")

                pages = {m.get("page") for m in all_metadata if m.get("page") is not None}
                if pages:
                    info["total_pages"] = len(pages)

                sheets = {m.get("sheet") for m in all_metadata if m.get("sheet") is not None}
                if sheets:
                    info["sheets"] = sorted(str(s) for s in sheets)

            return info
        except Exception as e:
            logger.error(f"[RETRIEVAL_V2] Failed to get metadata for {document_id}: {e}")
            return {"success": False, "document_id": document_id, "error": str(e)}

    def get_table_of_contents(self, document_id: str, user_id: str | None = None) -> dict:
        """The document's section outline (h1/h2/h3 headings + the page each
        was seen on), captured at index time from PDF/docx/txt structure —
        see langchain_structure.split_markdown_preserving_structure. Meant
        to be browsed before retrieve() so a caller can target a specific
        section/table with `filters` instead of a blind top-k query.

        Falls back to the page/sheet coverage from get_document_info when no
        TOC was captured (csv/xlsx, pptx, or a document indexed before this
        was added)."""
        collection_name = self._collection_name(document_id)
        try:
            collection = self.chroma_client.get_collection(name=collection_name)
        except Exception:
            return {"success": False, "document_id": document_id, "error": f"Document not found in v2 vector store: {document_id}"}

        if not self._check_ownership(collection, user_id):
            return {"success": False, "document_id": document_id, "error": f"Document not found in v2 vector store: {document_id}"}

        raw_toc = (collection.metadata or {}).get("toc")
        if raw_toc:
            try:
                toc = json.loads(raw_toc)
                return {"success": True, "document_id": document_id, "toc": toc, "pipeline": "langchain_v2"}
            except (TypeError, ValueError) as e:
                logger.warning(f"[RETRIEVAL_V2] Malformed TOC metadata for {document_id}: {e}")

        info = self.get_document_info(document_id, user_id=user_id)
        if not info.get("success"):
            return info
        return {
            "success": True,
            "document_id": document_id,
            "toc": [],
            "pages": info.get("total_pages"),
            "sheets": info.get("sheets"),
            "pipeline": "langchain_v2",
        }

    def list_chunks(
        self,
        document_id: str,
        user_id: str | None = None,
        chunk_id: str | None = None,
        page: int | None = None,
    ) -> dict:
        """All chunks of a document's v2 collection, in original
        chunk_index order, each with its full text + metadata + grounding —
        no query, scoring, or reranking. For inspection UIs (a document's
        chunk browser, citation click-through), not for answering
        questions; use retrieve() for that.

        `chunk_id`/`page` narrow to a single chunk or one page's chunks
        when given; omit both to list the whole document.
        """
        collection_name = self._collection_name(document_id)
        try:
            collection = self.chroma_client.get_collection(name=collection_name)
        except Exception:
            return {"success": False, "document_id": document_id, "error": f"Document not found in v2 vector store: {document_id}"}

        if not self._check_ownership(collection, user_id):
            return {"success": False, "document_id": document_id, "error": f"Document not found in v2 vector store: {document_id}"}

        try:
            docs = self._load_all_documents(collection)
            docs.sort(key=lambda d: d.metadata.get("chunk_index") or 0)

            if chunk_id is not None:
                docs = [d for d in docs if d.metadata.get("chunk_id") == chunk_id]
            if page is not None:
                docs = [d for d in docs if d.metadata.get("page") == page]

            chunks = [
                {
                    "text": d.page_content,
                    "metadata": dict(d.metadata),
                    "grounding": self._grounding(d.metadata),
                }
                for d in docs
            ]

            return {
                "success": True,
                "document_id": document_id,
                "chunks": chunks,
                "chunks_count": len(chunks),
                "pipeline": "langchain_v2",
            }
        except Exception as e:
            logger.exception(f"[RETRIEVAL_V2] list_chunks failed for document_id={document_id}")
            return {"success": False, "document_id": document_id, "error": str(e)}
