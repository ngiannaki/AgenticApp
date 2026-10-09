"""Hybrid retrieval pipeline (v2) over Postgres.

Reads the chunks written by backend/mcp_servers/indexing/langchain_pipeline.py
(schema `rag`, see backend/core/vector_store/pg_store.py) and combines two
search legs, both run inside Postgres:

- dense: pgvector cosine similarity against the query embedding
  (backend.config.get_langchain_embedding — Ollama in DEV, OpenAI in PROD;
  must match the model the document was indexed with)
- keyword: Postgres full-text search over a generated `tsvector` column
  (any query word may match, ranked with ts_rank_cd) — catches exact
  keywords, IDs and numbers that embeddings blur

The two ranked lists are merged with weighted reciprocal-rank fusion (the
same fusion LangChain's EnsembleRetriever used here before), then (when
enabled) a HuggingFace cross-encoder reranks the fused candidates down to
`top_k`, and adjacent chunks are spliced in for citation context.

Every returned chunk carries a `grounding` block (document_name, source,
page/sheet/row when the original loader captured one, chunk_id, ...) so a
caller can cite exactly where an answer came from.
"""

from __future__ import annotations

import logging
import re
import time
from functools import lru_cache
from typing import Any

from langchain_core.documents import Document

from backend.config import get_langchain_embedding, settings
from backend.core.vector_store.pg_store import PgVectorStore

logger = logging.getLogger(__name__)

# Reciprocal-rank-fusion constant (the standard 60, as in EnsembleRetriever).
_RRF_K = 60


@lru_cache(maxsize=2)
def _get_cross_encoder(model_name: str):
    """Cached so the (heavyweight) HuggingFace cross-encoder model is loaded
    once per process, not once per query."""
    from langchain_community.cross_encoders import HuggingFaceCrossEncoder

    logger.info(f"[RETRIEVAL_V2] Loading cross-encoder {model_name}")
    return HuggingFaceCrossEncoder(model_name=model_name)


def _rerank(model_name: str, query: str, docs: list[Document], top_n: int) -> list[Document]:
    """Order `docs` by cross-encoder relevance to `query`, keep `top_n`, and
    record each kept doc's score as metadata["relevance_score"]. (LangChain's
    CrossEncoderReranker does the same ordering but throws the scores away.)"""
    scores = _get_cross_encoder(model_name).score([(query, d.page_content) for d in docs])
    ranked = sorted(zip(docs, scores), key=lambda pair: pair[1], reverse=True)[:top_n]
    for doc, score in ranked:
        doc.metadata["relevance_score"] = float(score)
    return [doc for doc, _ in ranked]


def _not_found(document_id: str | None, query: str | None = None) -> dict:
    # Same message for "doesn't exist" and "belongs to someone else", so ids
    # can't be probed.
    result: dict[str, Any] = {
        "success": False,
        "document_id": document_id,
        "error": f"Document not found in vector store: {document_id}",
    }
    if query is not None:
        result["query"] = query
    return result


def _row_to_document(row: dict[str, Any]) -> Document:
    meta = dict(row["metadata"] or {})
    meta.setdefault("chunk_id", row["chunk_id"])
    meta.setdefault("chunk_index", row["chunk_index"])
    meta.setdefault("document_id", row["document_id"])
    return Document(page_content=row["content"], metadata=meta)


_TABLE_SEP_LINE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def _join_table_parts(parts: list[dict], hit_part: int, max_chars: int) -> str:
    """Rebuild a table split over several chunks: the first part as stored
    (section/caption lines + header), then only the data rows of the others
    (each part repeats the header). Past max_chars, keeps the parts nearest
    the hit and says how many rows were left out."""
    parts = sorted(parts, key=lambda r: (r["metadata"] or {}).get("table_part") or 0)
    rows_of = []
    for i, part in enumerate(parts):
        lines = part["content"].splitlines()
        if i:
            sep = next((n for n, ln in enumerate(lines) if _TABLE_SEP_LINE.match(ln.strip())), None)
            lines = lines[sep + 1:] if sep is not None else lines
        rows_of.append(lines)

    hit = min(max(hit_part - 1, 0), len(parts) - 1)
    order = sorted(range(len(parts)), key=lambda i: abs(i - hit))
    keep = {0, hit}  # the header part and the part that matched, always
    size = sum(len("\n".join(rows_of[i])) for i in keep)
    for i in order:
        if i in keep:
            continue
        extra = len("\n".join(rows_of[i]))
        if size + extra > max_chars:
            break
        keep.add(i)
        size += extra
    lines: list[str] = []
    for i in range(len(parts)):
        if i in keep:
            lines.extend(rows_of[i])
        elif not lines or lines[-1] != "| … (rows omitted) |":
            lines.append("| … (rows omitted) |")
    return "\n".join(lines)


def fuse_rankings(
    ranked_lists: list[list[dict[str, Any]]], weights: list[float]
) -> list[dict[str, Any]]:
    """Weighted reciprocal-rank fusion of several best-first result lists,
    deduplicated by chunk_id. Each returned row gets `rrf_score`, plus
    `dense_score` when it came from the first (dense) list."""
    fused: dict[str, dict[str, Any]] = {}
    for list_index, (rows, weight) in enumerate(zip(ranked_lists, weights)):
        for rank, row in enumerate(rows, start=1):
            entry = fused.get(row["chunk_id"])
            if entry is None:
                entry = {**row, "rrf_score": 0.0}
                fused[row["chunk_id"]] = entry
            entry["rrf_score"] += weight / (_RRF_K + rank)
            if list_index == 0:
                entry["dense_score"] = row.get("score")
    return sorted(fused.values(), key=lambda r: r["rrf_score"], reverse=True)


class LangchainRetrievalPipelineV2:
    """Hybrid (pgvector + full-text) retrieval with cross-encoder reranking."""

    def __init__(self, store: PgVectorStore | None = None):
        self.cfg = settings.rag_v2
        self.store = store or PgVectorStore()
        self.embedding_model = settings.embedding_model
        self.embeddings = get_langchain_embedding(self.embedding_model)
        logger.info("LangchainRetrievalPipelineV2 initialized (embedding model %s)", self.embedding_model)

    # --- access ---------------------------------------------------------------

    def _owned_document(self, document_id: str, user_id: str | None) -> dict | None:
        """The rag.documents row, only if `user_id` owns it."""
        if not user_id:
            return None
        doc = self.store.get_documents([document_id]).get(document_id)
        if doc is None or doc["user_id"] != user_id:
            if doc is not None:
                logger.warning(f"[RETRIEVAL_V2] Denied access to {document_id} for user_id={user_id!r}")
            return None
        return doc

    def _model_mismatch(self, doc: dict) -> str | None:
        if doc["embedding_model"] != self.embedding_model:
            return (
                f"Document {doc['document_id']} was indexed with embedding model "
                f"'{doc['embedding_model']}', but this server queries with '{self.embedding_model}'. "
                "Re-index it (scripts/reindex_all.py) to search it with the current model."
            )
        return None

    # --- grounding ------------------------------------------------------------

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
            **({"table_id": metadata["table_id"], "table_pages": metadata.get("table_pages")} if metadata.get("table_id") else {}),
        }

    def _expand_neighbors(self, docs: list[Document]) -> list[tuple[Document, bool]]:
        """Give each hit more context. A part of a table that was split into
        several chunks comes back as the whole table (up to
        table_expand_max_chars, centred on the hit), and further hits on the
        same table are dropped. Any other hit gets ±neighbor_window adjacent
        chunks (by chunk_index) spliced in. Returns (doc, expanded) pairs —
        `doc.page_content` is replaced with the merged text when expansion
        happened; the hit's own metadata is kept."""
        window = self.cfg.neighbor_window if self.cfg.neighbor_expansion else 0
        if not docs:
            return []

        wanted: dict[str, set[int]] = {}
        tables: dict[tuple[str, str], list[dict]] = {}
        for doc in docs:
            idx, meta = doc.metadata.get("chunk_index"), doc.metadata
            if meta.get("table_id") and (meta.get("table_parts") or 1) > 1:
                key = (meta["document_id"], meta["table_id"])
                if key not in tables:
                    tables[key] = self.store.get_chunks(meta["document_id"], metadata={"table_id": meta["table_id"]})
            elif idx is not None and window:
                wanted.setdefault(meta["document_id"], set()).update(range(idx - window, idx + window + 1))
        by_key: dict[tuple[str, int], str] = {}
        for document_id, indexes in wanted.items():
            for row in self.store.get_chunks(document_id, chunk_indexes=sorted(indexes)):
                by_key[(document_id, row["chunk_index"])] = row["content"]

        out: list[tuple[Document, bool]] = []
        tables_returned: set[tuple[str, str]] = set()
        for doc in docs:
            meta, idx = doc.metadata, doc.metadata.get("chunk_index")
            key = (meta.get("document_id"), meta.get("table_id"))
            if key in tables:
                if key in tables_returned:
                    continue
                tables_returned.add(key)
                text = _join_table_parts(tables[key], meta.get("table_part") or 1, self.cfg.table_expand_max_chars)
                out.append((Document(page_content=text, metadata=meta), True))
                continue
            if idx is None or not window:
                out.append((doc, False))
                continue
            document_id = meta["document_id"]
            pieces, expanded = [], False
            for offset in range(-window, window + 1):
                text = doc.page_content if offset == 0 else by_key.get((document_id, idx + offset))
                if text is None:
                    continue
                if offset != 0:
                    expanded = True
                pieces.append(text)
            out.append((Document(page_content="\n\n".join(pieces), metadata=meta), True) if expanded else (doc, False))
        return out

    # --- search ---------------------------------------------------------------

    def _search(
        self, document_ids: list[str], query: str, top_k: int | None, filters: dict | None
    ) -> tuple[list[dict], bool, bool, dict[str, int]]:
        """Hybrid search over `document_ids` (already ownership-checked).
        Returns (source_chunks, used_keyword_leg, reranked, timings_ms) —
        timings_ms is the milliseconds each stage took (embed, dense,
        keyword, fuse, rerank, expand), for the app's usage dashboard."""
        top_k = min(top_k or self.cfg.default_top_k, self.cfg.max_top_k)
        fetch_k = min(max(top_k * self.cfg.fetch_k_multiplier, 10), self.cfg.max_fetch_k)

        timings: dict[str, int] = {}
        mark = time.perf_counter()

        def lap(stage: str) -> None:
            nonlocal mark
            now = time.perf_counter()
            timings[stage] = round((now - mark) * 1000)
            mark = now

        query_vector = self.embeddings.embed_query(query)
        lap("embed")
        dense = self.store.dense_search(document_ids, query_vector, fetch_k, filters)
        lap("dense")
        keyword = self.store.keyword_search(document_ids, query, fetch_k, filters)
        lap("keyword")
        fused = fuse_rankings([dense, keyword], [self.cfg.dense_weight, self.cfg.sparse_weight])
        lap("fuse")

        candidates = [_row_to_document(r) for r in fused]
        for doc, row in zip(candidates, fused):
            doc.metadata["relevance_score"] = row.get("dense_score")

        reranked = False
        if self.cfg.use_reranker and candidates:
            try:
                candidates = _rerank(self.cfg.rerank_model, query, candidates, top_k)
                reranked = True
            except Exception as e:  # noqa: BLE001 — degrade to fused order, don't fail the search
                logger.warning(f"[RETRIEVAL_V2] Reranker unavailable, skipping: {e}")
        lap("rerank")
        docs = candidates[:top_k]

        expanded_docs = self._expand_neighbors(docs)
        source_chunks = []
        for doc, expanded in expanded_docs:
            metadata = dict(doc.metadata)
            score = metadata.pop("relevance_score", None)
            source_chunks.append(
                {
                    "text": doc.page_content,
                    "score": float(score) if score is not None else None,
                    "metadata": metadata,
                    "grounding": self._grounding(metadata, expanded=expanded),
                }
            )
        lap("expand")
        return source_chunks, bool(keyword), reranked, timings

    def retrieve(
        self,
        document_id: str,
        query: str,
        top_k: int | None = None,
        user_id: str | None = None,
        filters: dict | None = None,
    ) -> dict:
        """Hybrid-retrieve + rerank chunks for `query` from one document.

        Does not synthesize an answer: returns source chunks only, each with
        `text`, `score` (reranker relevance score when reranking ran, else
        the vector similarity score, None for keyword-only hits),
        `metadata`, and a `grounding` block.

        `filters` restricts candidates to exact metadata matches before
        ranking — e.g. {"chunk_content_type": "table"} to only search table
        chunks, {"page": 3} for one page, {"h1": "..."} for one section.
        Applied identically to both search legs.
        """
        if not query or not query.strip():
            return {"success": False, "document_id": document_id, "error": "Query parameter is required and cannot be empty"}

        doc = self._owned_document(document_id, user_id)
        if doc is None:
            return _not_found(document_id, query)
        mismatch = self._model_mismatch(doc)
        if mismatch:
            return {"success": False, "document_id": document_id, "query": query, "error": mismatch}

        try:
            source_chunks, used_keyword, reranked, timings = self._search([document_id], query, top_k, filters)
            logger.info(
                f"[RETRIEVAL_V2] document_id={document_id} hybrid={used_keyword} reranked={reranked} -> {len(source_chunks)} chunks"
            )
            return {
                "success": True,
                "document_id": document_id,
                "query": query,
                "source_chunks": source_chunks,
                "chunks_count": len(source_chunks),
                "pipeline": "langchain_v2",
                "retrieval_mode": "hybrid" if used_keyword else "dense",
                "reranked": reranked,
                "filters": filters,
                "timings_ms": timings,
            }
        except Exception as e:
            logger.exception(f"[RETRIEVAL_V2] Retrieval failed for document_id={document_id}")
            return {"success": False, "document_id": document_id, "query": query, "error": str(e)}

    def search_documents(
        self,
        query: str,
        user_id: str | None,
        document_ids: list[str] | None = None,
        top_k: int | None = None,
        filters: dict | None = None,
    ) -> dict:
        """Same hybrid search as retrieve(), across several of the user's
        documents at once — or all of them when `document_ids` is empty.
        Each chunk's grounding says which document it came from."""
        if not query or not query.strip():
            return {"success": False, "error": "Query parameter is required and cannot be empty"}
        if not user_id:
            return {"success": False, "error": "user_id is required"}

        skipped: list[dict] = []
        if document_ids:
            docs = self.store.get_documents(document_ids)
            for doc_id in document_ids:
                doc = docs.get(doc_id)
                if doc is None or doc["user_id"] != user_id:
                    return _not_found(doc_id, query)
            searchable = []
            for doc_id in document_ids:
                mismatch = self._model_mismatch(docs[doc_id])
                if mismatch:
                    skipped.append({"document_id": doc_id, "reason": mismatch})
                else:
                    searchable.append(doc_id)
        else:
            searchable = self.store.list_user_documents(user_id, self.embedding_model)

        if not searchable:
            return {
                "success": True,
                "query": query,
                "source_chunks": [],
                "chunks_count": 0,
                "documents_searched": 0,
                "skipped": skipped,
                "pipeline": "langchain_v2",
            }

        try:
            source_chunks, used_keyword, reranked, timings = self._search(searchable, query, top_k, filters)
            logger.info(
                f"[RETRIEVAL_V2] search across {len(searchable)} doc(s) hybrid={used_keyword} reranked={reranked} -> {len(source_chunks)} chunks"
            )
            return {
                "success": True,
                "query": query,
                "source_chunks": source_chunks,
                "chunks_count": len(source_chunks),
                "documents_searched": len(searchable),
                "skipped": skipped,
                "pipeline": "langchain_v2",
                "retrieval_mode": "hybrid" if used_keyword else "dense",
                "reranked": reranked,
                "filters": filters,
                "timings_ms": timings,
            }
        except Exception as e:
            logger.exception("[RETRIEVAL_V2] Cross-document search failed")
            return {"success": False, "query": query, "error": str(e)}

    # --- document info ----------------------------------------------------------

    def get_document_info(self, document_id: str, user_id: str | None = None) -> dict:
        """Metadata/statistics for an indexed document."""
        doc = self._owned_document(document_id, user_id)
        if doc is None:
            return _not_found(document_id)
        try:
            coverage = self.store.coverage(document_id)
            info: dict = {
                "success": True,
                "document_id": document_id,
                "document_name": doc["document_name"] or "Untitled",
                "content_type": doc["content_type"],
                "embedding_model": doc["embedding_model"],
                "total_chunks": coverage["total_chunks"],
                "pipeline": "langchain_v2",
            }
            if coverage.get("total_pages"):
                info["total_pages"] = coverage["total_pages"]
            if coverage.get("sheets"):
                info["sheets"] = sorted(str(s) for s in coverage["sheets"])
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
        TOC was captured (csv/xlsx, pptx)."""
        doc = self._owned_document(document_id, user_id)
        if doc is None:
            return _not_found(document_id)
        if doc.get("toc"):
            return {"success": True, "document_id": document_id, "toc": doc["toc"], "pipeline": "langchain_v2"}

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
        """All chunks of a document, in original chunk_index order, each with
        its full text + metadata + grounding — no query, scoring, or
        reranking. For inspection UIs (a document's chunk browser, citation
        click-through), not for answering questions; use retrieve() for that.

        `chunk_id`/`page` narrow to a single chunk or one page's chunks
        when given; omit both to list the whole document.
        """
        if self._owned_document(document_id, user_id) is None:
            return _not_found(document_id)
        try:
            rows = self.store.get_chunks(document_id, chunk_id=chunk_id, page=page)
            chunks = []
            for row in rows:
                metadata = _row_to_document(row).metadata
                chunks.append({"text": row["content"], "metadata": metadata, "grounding": self._grounding(metadata)})
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
