"""Citation stamping and rendering: a raw chunk id must never reach the user."""
from __future__ import annotations

import json

import pytest

from backend.core.citations import _collect_and_stamp_citations, _render_citations

DOC = "a9507fec-007b-4c77-93a9-c2637b185f33"
ID1 = f"{DOC}-v2-75-4e933c39"
ID2 = f"{DOC}-v2-76-404291d3"
CITATIONS = [
    {"number": 1, "text": "one", "grounding": {"chunk_id": ID1}},
    {"number": 2, "text": "two", "grounding": {"chunk_id": ID2}},
]


def render(answer: str) -> tuple[str, list[int]]:
    text, cited = _render_citations(answer, CITATIONS)
    return text, [c["number"] for c in cited]


def test_the_exact_tag_becomes_a_numbered_marker() -> None:
    assert render(f'Revenue grew <citation id="{ID1}"/>.') == ("Revenue grew [1].", [1])


@pytest.mark.parametrize(
    "written",
    [
        f"cite{ID1}",  # the form seen in the chat: "cite" glued to the id
        f"cite {ID1}",
        f"cite:{ID1}",
        f"[cite: {ID1}]",
        f"(cite_as={ID1})",
        f'cite_as="{ID1}"',
        f"<citation id={ID1}>",
        f"<citation id='{ID1}'/>",
        ID1,  # the bare id
    ],
)
def test_a_mangled_reference_to_a_known_chunk_still_renders_as_a_marker(written: str) -> None:
    text, numbers = render(f"Higher confidence helped. {written}\nNext point.")
    assert ID1 not in text and "cite" not in text.lower()
    assert text == "Higher confidence helped. [1]\nNext point."
    assert numbers == [1]


def test_markers_are_numbered_in_reading_order_across_forms() -> None:
    answer = f'First cite{ID2} then <citation id="{ID1}"/> and again [cite: {ID2}].'
    text, numbers = render(answer)
    assert text == "First [1] then [2] and again [1]."
    assert numbers == [1, 2]  # Sources follow the order they first appear in the answer


def test_unknown_ids_are_dropped_and_never_shown() -> None:
    text, numbers = render('A <citation id="made-up"/> claim.')
    assert text == "A  claim."
    assert numbers == [1, 2]  # no valid tag at all: fall back to listing every collected source


def test_an_answer_with_no_tags_keeps_all_sources() -> None:
    assert render("No tags here.") == ("No tags here.", [1, 2])


def test_the_text_around_a_reference_is_left_alone() -> None:
    text, _ = render(f"See cited work.  cite{ID1}  Done (really).")
    assert text == "See cited work.  [1]  Done (really)."


def test_multi_document_search_results_are_stamped_too() -> None:
    result = json.dumps({"source_chunks": [{"text": "x", "grounding": {"chunk_id": ID1}}]})
    citations: list = []
    stamped = _collect_and_stamp_citations(citations, {}, "retrieval_search_my_documents_v2", True, result)
    assert json.loads(stamped)["source_chunks"][0]["cite_as"] == f'<citation id="{ID1}"/>'
    assert [c["number"] for c in citations] == [1]
    # ...and other tools are left alone
    assert _collect_and_stamp_citations([], {}, "db_query_data", True, result) == result


# --- ids the current turn doesn't know -------------------------------------------------

STRANGER = "fbabaf01-a88b-4a97-8f71-a723f33d3433-v2-406-d13a64d0"  # from a real chat


def test_an_unknown_chunk_id_is_removed_not_shown() -> None:
    text, _ = render(f"The model uses SHAP. cite{STRANGER}\nNext. [cite: {STRANGER}] End <citation id={STRANGER}>.")
    assert STRANGER not in text and "cite" not in text.lower()
    assert text == "The model uses SHAP. \nNext.  End ."


def test_an_id_from_an_earlier_turn_resolves_to_its_source() -> None:
    from backend.core.citations import remember_citations

    history: dict = {}
    remember_citations(history, [{"number": 1, "text": "old", "grounding": {"chunk_id": STRANGER}}])
    text, cited = _render_citations(f"As before cite{STRANGER}.", CITATIONS, history)
    assert text == "As before [1]."
    assert [c["grounding"]["chunk_id"] for c in cited] == [STRANGER]  # this turn's own chunks are not listed


def test_this_turns_chunk_wins_over_a_remembered_one_with_the_same_id() -> None:
    history = {ID1: {"number": 9, "text": "stale", "grounding": {"chunk_id": ID1}}}
    _, cited = _render_citations(f"x cite{ID1}", CITATIONS, history)
    assert cited[0]["text"] == "one"


def test_remembered_chunks_are_bounded_and_keep_the_newest() -> None:
    from backend.core import citations as mod

    history: dict = {}
    batch = [{"number": i, "text": "", "grounding": {"chunk_id": f"c{i}"}} for i in range(mod._MAX_REMEMBERED_CHUNKS + 25)]
    mod.remember_citations(history, batch)
    assert len(history) == mod._MAX_REMEMBERED_CHUNKS
    assert "c0" not in history and f"c{mod._MAX_REMEMBERED_CHUNKS + 24}" in history
