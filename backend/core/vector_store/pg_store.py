"""Document chunks + embeddings in Postgres (schema `rag`, see
backend/migrations/0001_init.sql). Replaces the per-document Chroma
collections the v2 pipeline used to write.

One shared table for every document instead of one collection each:
ownership lives on `rag.documents.user_id`, so searching several (or all)
of a user's documents at once is a single filtered query, and both search
legs run in the database with real indexes — dense via pgvector cosine
distance, keyword via a generated `tsvector` column — instead of loading a
whole collection into memory to rebuild BM25 on every query.

Sync on purpose (called from the synchronous FastMCP tool functions in the
indexing/retrieval servers).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from psycopg_pool import ConnectionPool

from backend.core.db import get_pool

# Postgres full-text query terms: word characters only, so nothing in a user's
# query can carry tsquery syntax (& | ! : * parentheses) into to_tsquery.
_TERM_RE = re.compile(r"\w+", re.UNICODE)
_MAX_QUERY_TERMS = 32


@dataclass
class ChunkRecord:
    chunk_id: str
    chunk_index: int
    content: str
    metadata: dict[str, Any]
    embedding: Sequence[float]


def _vector_literal(values: Iterable[float]) -> str:
    """pgvector's text input format ('[1,2,3]'), cast with ::vector in SQL —
    avoids needing the `pgvector` adapter package just to send a list."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _filter_json(filters: dict[str, Any] | None) -> str | None:
    """Exact-match metadata filters (page, sheet, h1, chunk_content_type...)
    as a jsonb containment document; None values are ignored."""
    if not filters:
        return None
    clean = {k: v for k, v in filters.items() if v is not None}
    return json.dumps(clean) if clean else None


def keyword_tsquery(query: str) -> str | None:
    """OR of the query's distinct words, e.g. 'revenue | 2023 | growth' —
    BM25-like recall (any term matches, more matches rank higher) rather
    than websearch_to_tsquery's all-terms-required AND."""
    terms: list[str] = []
    for term in _TERM_RE.findall(query.lower()):
        if term not in terms:
            terms.append(term)
        if len(terms) >= _MAX_QUERY_TERMS:
            break
    return " | ".join(terms) if terms else None


class PgVectorStore:
    def __init__(self, pool: ConnectionPool | None = None) -> None:
        self._pool = pool

    @property
    def pool(self) -> ConnectionPool:
        return self._pool or get_pool()

    # --- writes ---------------------------------------------------------------

    def replace_document(
        self,
        *,
        document_id: str,
        user_id: str,
        document_name: str | None,
        content_type: str | None,
        embedding_model: str,
        toc: list[dict] | None,
        chunks: list[ChunkRecord],
    ) -> None:
        """(Re)write a document's chunks atomically: a re-index replaces the
        old chunks instead of mixing with them."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM rag.documents WHERE document_id = %s", (document_id,))
            conn.execute(
                "INSERT INTO rag.documents"
                " (document_id, user_id, document_name, content_type, embedding_model, toc, chunk_count)"
                " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)",
                (
                    document_id,
                    user_id,
                    document_name,
                    content_type,
                    embedding_model,
                    json.dumps(toc) if toc else None,
                    len(chunks),
                ),
            )
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO rag.chunks (chunk_id, document_id, chunk_index, content, metadata, embedding)"
                    " VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector)",
                    [
                        (
                            c.chunk_id,
                            document_id,
                            c.chunk_index,
                            c.content,
                            json.dumps(c.metadata, default=str),
                            _vector_literal(c.embedding),
                        )
                        for c in chunks
                    ],
                )

    def delete_document(self, document_id: str) -> bool:
        with self.pool.connection() as conn:
            cur = conn.execute("DELETE FROM rag.documents WHERE document_id = %s", (document_id,))
            return cur.rowcount > 0

    # --- reads ------------------------------------------------------------------

    def get_documents(self, document_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not document_ids:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT document_id, user_id, document_name, content_type, embedding_model, toc, chunk_count"
                " FROM rag.documents WHERE document_id = ANY(%s)",
                (list(document_ids),),
            ).fetchall()
        return {r["document_id"]: r for r in rows}

    def list_user_documents(self, user_id: str, embedding_model: str) -> list[str]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT document_id FROM rag.documents WHERE user_id = %s AND embedding_model = %s",
                (user_id, embedding_model),
            ).fetchall()
        return [r["document_id"] for r in rows]

    def dense_search(
        self,
        document_ids: Sequence[str],
        query_embedding: Sequence[float],
        k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Top-k chunks by cosine similarity (score = 1 - cosine distance)."""
        flt = _filter_json(filters)
        vec = _vector_literal(query_embedding)
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT chunk_id, document_id, chunk_index, content, metadata,"
                "       1 - (embedding <=> %(vec)s::vector) AS score"
                "  FROM rag.chunks"
                " WHERE document_id = ANY(%(ids)s)"
                "   AND (%(flt)s::jsonb IS NULL OR metadata @> %(flt)s::jsonb)"
                " ORDER BY embedding <=> %(vec)s::vector"
                " LIMIT %(k)s",
                {"vec": vec, "ids": list(document_ids), "flt": flt, "k": k},
            ).fetchall()

    def keyword_search(
        self,
        document_ids: Sequence[str],
        query: str,
        k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Top-k chunks by full-text rank (any query word may match)."""
        tsq = keyword_tsquery(query)
        if tsq is None:
            return []
        flt = _filter_json(filters)
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT chunk_id, document_id, chunk_index, content, metadata,"
                "       ts_rank_cd(tsv, q) AS score"
                "  FROM rag.chunks, to_tsquery('simple', %(tsq)s) AS q"
                " WHERE document_id = ANY(%(ids)s)"
                "   AND tsv @@ q"
                "   AND (%(flt)s::jsonb IS NULL OR metadata @> %(flt)s::jsonb)"
                " ORDER BY score DESC"
                " LIMIT %(k)s",
                {"tsq": tsq, "ids": list(document_ids), "flt": flt, "k": k},
            ).fetchall()

    def get_chunks(
        self,
        document_id: str,
        *,
        chunk_indexes: Sequence[int] | None = None,
        chunk_id: str | None = None,
        page: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """A document's chunks in chunk_index order, optionally narrowed
        (`metadata`: exact-match JSONB containment, e.g. {"table_id": "p3-t1"})."""
        clauses = ["document_id = %s"]
        params: list[Any] = [document_id]
        if chunk_indexes is not None:
            clauses.append("chunk_index = ANY(%s)")
            params.append(list(chunk_indexes))
        if chunk_id is not None:
            clauses.append("chunk_id = %s")
            params.append(chunk_id)
        if page is not None:
            clauses.append("metadata @> %s::jsonb")
            params.append(json.dumps({"page": page}))
        if metadata:
            clauses.append("metadata @> %s::jsonb")
            params.append(json.dumps(metadata))
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT chunk_id, document_id, chunk_index, content, metadata FROM rag.chunks"
                f" WHERE {' AND '.join(clauses)} ORDER BY chunk_index",
                params,
            ).fetchall()

    def coverage(self, document_id: str) -> dict[str, Any]:
        """Distinct pages / sheets a document's chunks came from."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) AS total_chunks,"
                "       count(DISTINCT metadata->'page') FILTER (WHERE metadata ? 'page'"
                "             AND metadata->'page' <> 'null'::jsonb) AS total_pages,"
                "       array_remove(array_agg(DISTINCT metadata->>'sheet'), NULL) AS sheets"
                "  FROM rag.chunks WHERE document_id = %s",
                (document_id,),
            ).fetchone()
        return dict(row) if row else {"total_chunks": 0, "total_pages": 0, "sheets": []}
