"""Zero-LLM chunk context: section carried across pages, and the document /
sheet / slide line put in front of every chunk."""
from __future__ import annotations

from langchain_core.documents import Document

from backend.config import settings
from backend.mcp_servers.indexing.langchain_pipeline import LangchainIndexPipelineV2, context_prefix
from backend.mcp_servers.indexing.langchain_structure import split_markdown_preserving_structure


def _page(n: int, text: str) -> Document:
    return Document(page_content=text, metadata={"page": n})


def _pipeline() -> LangchainIndexPipelineV2:
    pipeline = object.__new__(LangchainIndexPipelineV2)  # no DB / embedding client needed to split
    pipeline.cfg = settings.rag_v2
    return pipeline


def test_page_without_heading_keeps_the_section_it_continues() -> None:
    pages = [
        _page(1, "# Results\n\n## Revenue\n\nRevenue grew 12% in 2023."),
        _page(2, "Costs were flat over the same period."),
        _page(3, "Table 4: Costs\n\n| Item | Cost |\n|---|---|\n| Rent | 5 |"),
    ]
    chunks, _ = _pipeline()._split(pages, "pdf")
    by_page = {c.metadata["page"]: c for c in chunks}
    assert by_page[2].page_content.startswith("Section: Results > Revenue\n\n")
    assert (by_page[2].metadata["h1"], by_page[2].metadata["h2"]) == ("Results", "Revenue")
    assert by_page[3].page_content.startswith("Section: Results > Revenue\nTable: Table 4: Costs")


def test_new_heading_on_a_later_page_inherits_only_the_shallower_levels() -> None:
    pages = [
        _page(1, "# Results\n\n## Revenue\n\nRevenue grew."),
        _page(2, "## Costs\n\nCosts were flat."),  # new h2 under the same h1
        _page(3, "# Outlook\n\nNext year."),  # new h1: nothing below it carries over
    ]
    chunks, _ = _pipeline()._split(pages, "pdf")
    by_page = {c.metadata["page"]: c.metadata for c in chunks}
    assert (by_page[2]["h1"], by_page[2]["h2"]) == ("Results", "Costs")
    assert by_page[3]["h1"] == "Outlook" and "h2" not in by_page[3]


def test_standalone_document_needs_no_state() -> None:
    chunks, _ = split_markdown_preserving_structure(_page(1, "# A\n\ntext"), 1000, 0)
    assert chunks[0].page_content == "Section: A\n\ntext"


def test_context_prefix_per_format() -> None:
    assert context_prefix("report.pdf", {"page": 2}) == "Document: report.pdf\n"
    assert context_prefix("book.xlsx", {"sheet": "Q3", "row": 4}) == "Document: book.xlsx\nSheet: Q3\n"
    assert context_prefix("deck.pptx", {"slide": 3, "slide_title": "Risks"}) == "Document: deck.pptx\nSlide 3: Risks\n"
    assert context_prefix("deck.pptx", {"slide": 3}) == "Document: deck.pptx\nSlide 3\n"
    assert context_prefix("Untitled", {}) == ""
    assert context_prefix(None, {}) == ""
