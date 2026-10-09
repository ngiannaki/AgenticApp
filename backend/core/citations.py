"""Citation stamping and rendering, shared by the chat runtime
(backend/apps/api/streaming_agent.py) and the agents (backend/apps/agents).

Retrieval results get an opaque `<citation id="..."/>` tag per chunk; the
model copies tags into its answer; rendering turns known tags into [n]
markers plus a Sources list and silently drops unknown ones.
"""
from __future__ import annotations

import json
import re
from typing import Any


# Tools whose results carry citable chunks (matched by suffix: the chat prefixes
# the server name, e.g. "retrieval_search_my_documents_v2").
_CITABLE_TOOLS = ("retrieve_document_v2", "search_my_documents_v2")


def _collect_and_stamp_citations(
    citations: list[dict[str, Any]], chunk_numbers: dict[str, int], name: str, ok: bool, text: str
) -> str:
    """Pull grounding out of a retrieve_document_v2 tool result, fold any
    not-yet-seen chunk into `citations` (first-appearance order across the
    whole turn — a later call re-returning an already-cited chunk reuses its
    number rather than renumbering), and stamp a `<citation id="..."/>` tag
    directly into the JSON handed back to the model, carrying that chunk's
    own opaque `chunk_id`.

    The stamping — and the choice of an id tag over a bracket number — is
    what actually makes citations reliable. Asking the model to write "[n]"
    itself requires it to reconstruct which ordinal a chunk was first
    assigned, purely from memory, across however many retrieval calls it's
    made this turn — the rag-retrieval-strategy skill's retry loop can mean
    3-4 calls and dozens of candidate chunks, and that cross-call
    bookkeeping is exactly the kind of thing a smaller model gets wrong.
    Observed in practice: the model correctly identified and quoted the
    right chunk (a financial statement table, correctly stating "page 51"
    pulled from the TOC) but emitted a "[n]" marker that actually resolved
    to an unrelated chunk from an earlier, broader retrieval call that had
    been numbered "[n]" first (a decoy percentage table on a different
    page) — a plausible-looking number pointing at the wrong source.

    A `chunk_id` tag fixes this by turning citing into a copy task instead
    of arithmetic: the model reproduces a string it just read seconds
    earlier. It also fails *safe* on a mistake — a typo'd id matches no real
    chunk and is simply dropped (see _render_citations), whereas a typo'd
    number can silently land on a different real, wrong chunk, exactly as
    happened here.

    Returns the (possibly modified) text to actually store in conversation
    memory and show in the tool-result event, so the model reads the same
    stamped version this function computed from.
    """
    if not ok or not name.endswith(_CITABLE_TOOLS):
        return text
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return text

    stamped = False
    for chunk in data.get("source_chunks") or []:
        grounding = chunk.get("grounding") or {}
        chunk_id = grounding.get("chunk_id")
        if not chunk_id:
            continue
        number = chunk_numbers.get(chunk_id)
        if number is None:
            number = len(citations) + 1
            chunk_numbers[chunk_id] = number
            citations.append({"number": number, "text": chunk.get("text", ""), "grounding": grounding})
        chunk["cite_as"] = f'<citation id="{chunk_id}"/>'
        stamped = True

    return json.dumps(data) if stamped else text


_CITATION_TAG_RE = re.compile(r'<citation\s+id="([^"]*)"\s*/?>')


def _citation_reference_re(chunk_ids: list[str]) -> "re.Pattern[str]":
    """Matches either the exact `<citation id="..."/>` tag (group `tag_id`, any
    id) or a looser reference to a chunk id we really handed out (group
    `loose_id`): the id on its own, or after/inside some mangled form of the
    tag a model sometimes writes instead — `cite<id>`, `[cite: <id>]`,
    `<citation id=<id>>`, `cite_as="<id>"`. Chunk ids are long and unique
    (`<document>-v2-<n>-<hash>`), so matching known ids anywhere is safe, and
    it keeps a raw id from ever reaching the user."""
    known = "|".join(re.escape(i) for i in sorted(chunk_ids, key=len, reverse=True))
    # Whitespace is only consumed when it is part of the reference itself
    # (between the bracket/"cite" and the id, or before a closing "/>" or
    # bracket), never the space or newline around it.
    loose = (
        r"(?i:(?:[<\[(]\s*)?(?:cit(?:e|ation)(?:[_\s]*(?:as|id))?\s*[:=]?\s*)?)"
        r"[\"']?(?P<loose_id>" + known + r")[\"']?(?:\s*/?[>\])])?"
    ) if known else r"(?!x)x"
    return re.compile(r'<citation\s+id="(?P<tag_id>[^"]*)"\s*/?>|' + loose)


# A chunk id left in the answer that matches no chunk we can resolve (a typo, an
# invented one, or one from a conversation we no longer remember): shaped
# "<document uuid>-v2-<n>-<8 hex>", optionally wrapped in a mangled tag.
_STRAY_CHUNK_REF_RE = re.compile(
    r"(?i:(?:[<\[(]\s*)?(?:cit(?:e|ation)(?:[_\s]*(?:as|id))?\s*[:=]?\s*)?)"
    r"[\"']?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-v2-\d+-[0-9a-f]{8}[\"']?(?:\s*/?[>\])])?"
)

_MAX_REMEMBERED_CHUNKS = 400


def remember_citations(history: dict[str, dict[str, Any]], citations: list[dict[str, Any]]) -> None:
    """Keep the chunks cited this turn, so a later turn that reuses one of their
    ids (the model still sees earlier tool results) can still resolve it to a
    real source instead of leaving a raw id behind. Bounded, oldest dropped."""
    for c in citations:
        chunk_id = (c.get("grounding") or {}).get("chunk_id")
        if chunk_id:
            history.pop(chunk_id, None)
            history[chunk_id] = c
    while len(history) > _MAX_REMEMBERED_CHUNKS:
        history.pop(next(iter(history)))


def _render_citations(
    answer: str, citations: list[dict[str, Any]], earlier: dict[str, dict[str, Any]] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Turn the model's inline `<citation id="..."/>` tags into the
    user-facing answer text plus the filtered, display-numbered Sources
    list, in one pass.

    Per tag found in `answer`, in reading order:
    - Known id (matches a chunk actually stamped this turn, per `citations`)
      -> replaced with a clean "[n]" marker, where n is assigned fresh here
      — 1 for the first distinct chunk id encountered while scanning
      `answer` left to right, 2 for the next new one, and so on. This is
      deliberately *not* the chunk's original stamp-time number (its
      first-appearance order across all of this turn's tool calls, which
      can run into double digits after a few retries) — a turn that
      retrieved a dozen candidates but only ended up citing two of them
      should show "[1]" and "[2]" in the Sources strip, not e.g. "[4]" and
      "[7]" inherited from how many chunks came before them in tool
      results the user never sees.
    - Unknown id (typo, invented, or left over from stale context) -> the
      tag is silently removed. No raw markup or misleading marker is ever
      shown to the user.

    A citation never tagged anywhere in the answer is dropped from the
    Sources list entirely — `citations` deliberately holds every chunk every
    retrieve_document_v2 call returned this turn (so the list can't be
    hallucinated), but a turn that retried retrieval can mean dozens of
    candidates the answer never actually used; keeping only the tagged ones
    is what makes the strip read as "these support the answer" rather than
    "these were looked at".

    Falls back to returning every collected citation, un-renumbered, when
    the answer contains no tags at all, so a model that skipped the
    instruction still shows sources rather than none.

    `earlier` holds chunks cited in previous turns of the same conversation
    (see remember_citations): an id from one of them resolves like a current
    one. Any other chunk-id-shaped text left over is removed, so a raw id is
    never shown.
    """
    by_chunk_id = {**(earlier or {}), **{c["grounding"].get("chunk_id"): c for c in citations}}
    display_numbers: dict[str, int] = {}

    def _replace(match: "re.Match[str]") -> str:
        chunk_id = match.group("loose_id") or match.group("tag_id")
        if chunk_id not in by_chunk_id:
            return ""
        number = display_numbers.get(chunk_id)
        if number is None:
            number = len(display_numbers) + 1
            display_numbers[chunk_id] = number
        return f"[{number}]"

    clean_answer = _citation_reference_re([i for i in by_chunk_id if i]).sub(_replace, answer)
    clean_answer = _STRAY_CHUNK_REF_RE.sub("", clean_answer)

    if not display_numbers:
        return clean_answer, citations

    cited = [
        {**by_chunk_id[chunk_id], "number": number}
        for chunk_id, number in sorted(display_numbers.items(), key=lambda kv: kv[1])
    ]
    return clean_answer, cited
