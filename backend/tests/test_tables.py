"""Tables in RAG: cross-page stitching, table chunks with id/part/caption,
and retrieval returning the whole table for a hit on any part of it."""
from __future__ import annotations

from langchain_core.documents import Document

from backend.mcp_servers.indexing.langchain_structure import (
    split_markdown_preserving_structure,
    stitch_cross_page_tables,
)
from backend.mcp_servers.retrieval.langchain_pipeline import _join_table_parts
from backend.tests.test_pg_retrieval import _chunks, pipeline, store  # noqa: F401  (fixtures)


def _page(n: int, text: str) -> Document:
    return Document(page_content=text, metadata={"page": n})


def test_table_split_by_a_page_break_is_stitched() -> None:
    pages = [
        _page(1, "# Results\n\nTable 2: Revenue by region\n\n| Region | Revenue |\n|---|---|\n| EU | 10 |\n\nAnnual report 2025 · 1"),
        _page(2, "Annual report 2025\n| Region | Revenue |\n|---|---|\n| US | 20 |\n| ASIA | 5 |\n\nNext section text."),
    ]
    out = stitch_cross_page_tables(pages)
    assert "| US | 20 |" in out[0].page_content and "| ASIA | 5 |" in out[0].page_content
    assert out[0].page_content.count("| Region | Revenue |") == 1  # repeated header dropped
    assert "| US |" not in out[1].page_content and "Next section text." in out[1].page_content
    assert out[0].metadata["table_pages"] == [1, 2]

    chunks, _ = split_markdown_preserving_structure(out[0], chunk_size=1000, chunk_overlap=0)
    table = next(c for c in chunks if c.metadata["chunk_content_type"] == "table")
    assert table.page_content.startswith("Section: Results\nTable: Table 2: Revenue by region\n")
    assert table.metadata["table_id"] == "p1-t1" and table.metadata["table_pages"] == [1, 2]
    assert all("table_pages" not in c.metadata for c in chunks if c.metadata["chunk_content_type"] == "prose")


def test_continuation_without_repeated_header_keeps_its_first_row() -> None:
    pages = [_page(1, "| A | B |\n|---|---|\n| 1 | 2 |"), _page(2, "| 3 | 4 |\n|---|---|\n| 5 | 6 |")]
    out = stitch_cross_page_tables(pages)
    assert out[0].page_content.splitlines()[-2:] == ["| 3 | 4 |", "| 5 | 6 |"]
    # Different column count: not the same table.
    pages = [_page(1, "| A | B |\n|---|---|\n| 1 | 2 |"), _page(2, "| X | Y | Z |\n|---|---|---|\n| 1 | 2 | 3 |")]
    assert "X" not in stitch_cross_page_tables(pages)[0].page_content


def test_long_table_parts_and_rebuild() -> None:
    rows = "\n".join(f"| row{i} | {i} |" for i in range(200))
    doc = _page(4, f"Table 9: Big\n\n| Name | N |\n|---|---|\n{rows}")
    chunks, _ = split_markdown_preserving_structure(doc, chunk_size=300, chunk_overlap=0)
    parts = [c for c in chunks if c.metadata["chunk_content_type"] == "table"]
    assert len(parts) > 3 and {p.metadata["table_parts"] for p in parts} == {len(parts)}
    stored = [{"content": p.page_content, "metadata": p.metadata} for p in parts]
    full = _join_table_parts(stored, hit_part=2, max_chars=100_000)
    assert full.count("| Name | N |") == 1 and "| row0 | 0 |" in full and "| row199 | 199 |" in full
    capped = _join_table_parts(stored, hit_part=len(parts), max_chars=600)
    assert "| row199 | 199 |" in capped and "rows omitted" in capped and full.splitlines()[0] == capped.splitlines()[0]


def test_hit_on_one_part_returns_the_whole_table(store, pipeline) -> None:  # noqa: F811
    meta = lambda part: {"page": 3, "chunk_content_type": "table", "table_id": "p3-t1", "table_part": part, "table_parts": 3}  # noqa: E731
    store.replace_document(
        document_id="doc-tables", user_id="alice", document_name="t.pdf", content_type="pdf",
        embedding_model=pipeline.embedding_model, toc=None,
        chunks=_chunks("doc-tables", [
            ("Intro prose", {"page": 3, "chunk_content_type": "prose"}),
            ("Table: Fees\n| Item | Cost |\n|---|---|\n| setup | 1 |", meta(1)),
            ("Table: Fees\n| Item | Cost |\n|---|---|\n| finance fee | 2 |", meta(2)),
            ("Table: Fees\n| Item | Cost |\n|---|---|\n| support | 3 |", meta(3)),
        ]),
    )
    try:
        result = pipeline.retrieve("doc-tables", "finance fee", top_k=4, user_id="alice")
        tables = [c for c in result["source_chunks"] if c["grounding"].get("table_id") == "p3-t1"]
        assert len(tables) == 1  # parts of the same table collapse into one hit
        text = tables[0]["text"]
        assert "| setup | 1 |" in text and "| finance fee | 2 |" in text and "| support | 3 |" in text
        assert text.count("| Item | Cost |") == 1
    finally:
        store.delete_document("doc-tables")
