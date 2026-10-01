"""Postgres vector store + the hybrid retrieval pipeline on top of it.
Needs TEST_DATABASE_ADMIN_URL (see conftest.py). Uses tiny fake embeddings
and no cross-encoder, so no model downloads."""
from __future__ import annotations

import pytest

from backend.core.vector_store.pg_store import ChunkRecord, PgVectorStore, keyword_tsquery
from backend.mcp_servers.retrieval import langchain_pipeline
from backend.mcp_servers.retrieval.langchain_pipeline import LangchainRetrievalPipelineV2, fuse_rankings

# A 3-d "embedding space": finance, cooking, sport.
TOPIC_VECTORS = {"finance": [1.0, 0.0, 0.0], "cooking": [0.0, 1.0, 0.0], "sport": [0.0, 0.0, 1.0]}


class FakeEmbeddings:
    def embed_query(self, text: str) -> list[float]:
        for topic, vec in TOPIC_VECTORS.items():
            if topic in text.lower():
                return vec
        return [0.3, 0.3, 0.3]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]


def _chunks(document_id: str, texts: list[tuple[str, dict]]) -> list[ChunkRecord]:
    emb = FakeEmbeddings()
    return [
        ChunkRecord(
            chunk_id=f"{document_id}-{i}",
            chunk_index=i,
            content=text,
            metadata={"document_id": document_id, "document_name": f"{document_id}.pdf", "chunk_index": i, **meta},
            embedding=emb.embed_query(text),
        )
        for i, (text, meta) in enumerate(texts)
    ]


@pytest.fixture
def store(pg_pool) -> PgVectorStore:
    return PgVectorStore(pg_pool)


@pytest.fixture
def pipeline(store, monkeypatch) -> LangchainRetrievalPipelineV2:
    monkeypatch.setattr(langchain_pipeline, "get_langchain_embedding", lambda model: FakeEmbeddings())
    p = LangchainRetrievalPipelineV2(store=store)
    p.cfg = p.cfg.model_copy(update={"use_reranker": False, "neighbor_window": 1})
    return p


@pytest.fixture
def indexed(store, pipeline):
    model = pipeline.embedding_model
    store.replace_document(
        document_id="doc-alice-1", user_id="alice", document_name="a.pdf", content_type="pdf",
        embedding_model=model, toc=[{"level": 1, "title": "Revenue", "page": 2}],
        chunks=_chunks("doc-alice-1", [
            ("Intro about finance and quarterly revenue INV-2291", {"page": 1, "chunk_content_type": "prose"}),
            ("Revenue table for finance", {"page": 2, "chunk_content_type": "table"}),
            ("Cooking pasta recipes", {"page": 3, "chunk_content_type": "prose"}),
        ]),
    )
    store.replace_document(
        document_id="doc-alice-2", user_id="alice", document_name="b.pdf", content_type="pdf",
        embedding_model=model, toc=None,
        chunks=_chunks("doc-alice-2", [("Sport results of the season", {"sheet": "S1"})]),
    )
    store.replace_document(
        document_id="doc-bob-1", user_id="bob", document_name="c.pdf", content_type="pdf",
        embedding_model=model, toc=None,
        chunks=_chunks("doc-bob-1", [("Bob's private finance notes", {"page": 1})]),
    )
    store.replace_document(
        document_id="doc-alice-old", user_id="alice", document_name="old.pdf", content_type="pdf",
        embedding_model="some-other-model", toc=None,
        chunks=_chunks("doc-alice-old", [("finance from an old model", {"page": 1})]),
    )
    yield
    for doc_id in ("doc-alice-1", "doc-alice-2", "doc-bob-1", "doc-alice-old"):
        store.delete_document(doc_id)


def test_keyword_tsquery_strips_syntax() -> None:
    assert keyword_tsquery("Revenue & (growth) | 'INV-2291'!") == "revenue | growth | inv | 2291"
    assert keyword_tsquery("!!! ???") is None


def test_fuse_rankings_rewards_agreement() -> None:
    dense = [{"chunk_id": "a", "score": 0.9}, {"chunk_id": "b", "score": 0.5}]
    keyword = [{"chunk_id": "b", "score": 3.0}, {"chunk_id": "c", "score": 1.0}]
    fused = fuse_rankings([dense, keyword], [0.5, 0.5])
    assert [r["chunk_id"] for r in fused][0] == "b"
    assert {r["chunk_id"] for r in fused} == {"a", "b", "c"}


def test_dense_and_keyword_search(store, indexed) -> None:
    dense = store.dense_search(["doc-alice-1"], TOPIC_VECTORS["cooking"], 1)
    assert dense[0]["content"] == "Cooking pasta recipes"
    kw = store.keyword_search(["doc-alice-1"], "invoice INV-2291", 5)
    assert [r["chunk_index"] for r in kw] == [0]
    table_only = store.dense_search(["doc-alice-1"], TOPIC_VECTORS["finance"], 5, {"chunk_content_type": "table"})
    assert [r["chunk_index"] for r in table_only] == [1]


def test_replace_document_overwrites_chunks(store, pipeline, indexed) -> None:
    store.replace_document(
        document_id="doc-alice-2", user_id="alice", document_name="b.pdf", content_type="pdf",
        embedding_model=pipeline.embedding_model, toc=None,
        chunks=_chunks("doc-alice-2", [("new content", {})]),
    )
    assert [r["content"] for r in store.get_chunks("doc-alice-2")] == ["new content"]


def test_retrieve_owner_only(pipeline, indexed) -> None:
    ok = pipeline.retrieve("doc-alice-1", "finance revenue", top_k=2, user_id="alice")
    assert ok["success"] and ok["retrieval_mode"] == "hybrid"
    assert ok["source_chunks"][0]["grounding"]["document_id"] == "doc-alice-1"
    # Neighbor expansion splices adjacent chunk text in.
    assert any(c["grounding"]["expanded"] for c in ok["source_chunks"])

    for user in ("bob", None):
        denied = pipeline.retrieve("doc-alice-1", "finance", user_id=user)
        assert not denied["success"] and "not found" in denied["error"]
    missing = pipeline.retrieve("nope", "finance", user_id="alice")
    assert missing["error"] == denied["error"].replace("doc-alice-1", "nope")


def test_retrieve_with_filters(pipeline, indexed) -> None:
    result = pipeline.retrieve("doc-alice-1", "finance", user_id="alice", filters={"chunk_content_type": "table"})
    assert {c["grounding"]["chunk_content_type"] for c in result["source_chunks"]} == {"table"}


def test_model_mismatch_is_reported(pipeline, indexed) -> None:
    result = pipeline.retrieve("doc-alice-old", "finance", user_id="alice")
    assert not result["success"] and "re-index" in result["error"].lower()


def test_search_across_my_documents(pipeline, indexed) -> None:
    result = pipeline.search_documents("sport season", user_id="alice", top_k=3)
    assert result["success"]
    assert result["documents_searched"] == 2  # alice's two current-model docs; not bob's, not the old-model one
    found = {c["grounding"]["document_id"] for c in result["source_chunks"]}
    assert "doc-bob-1" not in found and "doc-alice-old" not in found
    assert result["source_chunks"][0]["grounding"]["document_id"] == "doc-alice-2"

    denied = pipeline.search_documents("finance", user_id="alice", document_ids=["doc-alice-1", "doc-bob-1"])
    assert not denied["success"]

    partly = pipeline.search_documents("finance", user_id="alice", document_ids=["doc-alice-1", "doc-alice-old"])
    assert partly["success"] and partly["documents_searched"] == 1 and len(partly["skipped"]) == 1


def test_info_toc_and_chunks(pipeline, indexed) -> None:
    info = pipeline.get_document_info("doc-alice-1", user_id="alice")
    assert info["total_chunks"] == 3 and info["total_pages"] == 3
    assert pipeline.get_document_info("doc-alice-2", user_id="alice")["sheets"] == ["S1"]
    assert pipeline.get_table_of_contents("doc-alice-1", user_id="alice")["toc"][0]["title"] == "Revenue"
    assert pipeline.get_table_of_contents("doc-alice-2", user_id="alice")["toc"] == []
    chunks = pipeline.list_chunks("doc-alice-1", user_id="alice", page=2)
    assert [c["metadata"]["chunk_index"] for c in chunks["chunks"]] == [1]
    assert not pipeline.list_chunks("doc-alice-1", user_id="bob")["success"]
