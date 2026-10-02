"""Extractor: pulls the values the user asks for out of their documents and
returns them as JSON only — never prose, whatever the request says
("summarize", a question, ...): a summary becomes {"summary": "..."}, an
answer becomes {"answer": "..."}. Every value carries its source."""
from __future__ import annotations

from typing import Any

from backend.apps.agents.base import AgentError, BaseAgent, Field, RunContext, parse_json

_PLANNER = (
    "You turn an extraction request into a list of fields to extract. Each field gets a snake_case key, "
    "a short description and a search query to find it in documents. If the request isn't a list of "
    'variables (e.g. "summarize the document" or a question), use one field such as "summary" or "answer". '
    'JSON: {"fields": [{"key": string, "description": string, "query": string}]}'
)
_EXTRACTOR = (
    "You extract values from document excerpts. For each requested field return the value exactly as "
    "supported by the excerpts (numbers as numbers, dates as ISO strings, lists as arrays), or null if not "
    "found — never guess. For every value cite the excerpt tag (e.g. E3) it came from and copy the exact "
    "words from that excerpt that support it (a short verbatim quote; for a table, the row). Output JSON only: "
    '{"<key>": {"value": <any>, "source": "E<n>" | null, "quote": string | null}}'
)


def _normalized(text: str) -> str:
    return " ".join(text.replace("|", " ").split()).lower()


class ExtractorAgent(BaseAgent):
    id = "extractor"
    name = "Extractor"
    description = (
        "Extracts the values you ask for from your documents and returns them as structured JSON "
        "— always JSON, even for summaries or questions — with the source of every value."
    )
    inputs = [
        Field("request", "textarea", "What to extract", required=True,
              help="e.g. invoice number, total amount, due date, supplier name"),
        Field("document_ids", "documents", "Documents (leave empty for all of yours)"),
    ]
    allowed_tools = {"retrieval_search_my_documents_v2"}
    examples = ["Return the contract parties, start date, end date, total value and payment terms"]

    async def run(self, ctx: RunContext, inputs: dict[str, Any]) -> None:
        await ctx.step("plan", "Working out what to extract")
        plan = await ctx.llm.json(_PLANNER, f"Request: {inputs['request']}")
        fields = [f for f in (plan or {}).get("fields", []) if isinstance(f, dict) and f.get("key")][:30]
        if not fields:
            raise AgentError("Couldn't work out which values to extract from that request.")
        await ctx.step("plan", f"{len(fields)} field(s)", ", ".join(f["key"] for f in fields))

        excerpts: list[dict] = []
        seen: set[str] = set()
        problems: set[str] = set()
        searched = 0
        for f in fields:
            await ctx.step("search", f"Searching for {f['key']}", f.get("query", ""))
            args: dict[str, Any] = {"query": f.get("query") or f.get("description") or f["key"], "top_k": 4}
            if inputs["document_ids"]:
                args["document_ids"] = inputs["document_ids"]
            result = await ctx.call_tool("retrieval_search_my_documents_v2", args)
            payload = result.data if isinstance(result.data, dict) else {}
            if not result.ok or not payload.get("success", False):
                problems.add((payload.get("error") or result.text or "search failed")[:300])
            searched = max(searched, payload.get("documents_searched") or 0)
            for skip in payload.get("skipped") or []:
                problems.add(skip.get("reason", "")[:300])
            for chunk in payload.get("source_chunks") or []:
                grounding = chunk.get("grounding") or {}
                chunk_id = grounding.get("chunk_id") or chunk.get("text", "")[:40]
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                # A whole table can be long; prose excerpts stay short.
                limit = 6000 if grounding.get("chunk_content_type") == "table" else 1500
                excerpts.append({"tag": f"E{len(excerpts) + 1}", "text": chunk.get("text", "")[:limit], "grounding": grounding})
        if not excerpts:
            problems.discard("")
            if problems:
                raise AgentError("Document search failed: " + "; ".join(sorted(problems)))
            if not searched:
                raise AgentError(
                    "You have no indexed documents to search (none uploaded yet, still indexing, "
                    "or indexed with a different embedding model — re-index them)."
                )
            raise AgentError(f"No relevant text was found in your {searched} document(s).")

        await ctx.step("extract", "Extracting values")
        listing = "\n\n".join(f"[{e['tag']}] ({e['grounding'].get('document_name')}) {e['text']}" for e in excerpts[:40])
        wanted = "\n".join(f"- {f['key']}: {f.get('description', '')}" for f in fields)
        raw = parse_json(await ctx.llm.text(_EXTRACTOR, f"Fields:\n{wanted}\n\nExcerpts:\n{listing}\n\nJSON only."))
        if not isinstance(raw, dict):
            raw = {}

        # Per-field citations: numbered like the chat's [n] (one number per
        # distinct excerpt), with the supporting quote checked against the
        # excerpt text so a made-up quote is flagged rather than shown as fact.
        by_tag = {e["tag"]: e for e in excerpts}
        values: dict[str, Any] = {}
        sources: dict[str, Any] = {}
        citations: list[dict[str, Any]] = []
        numbers: dict[str, int] = {}
        for f in fields:
            item = raw.get(f["key"])
            value = item.get("value") if isinstance(item, dict) else item
            values[f["key"]] = value
            excerpt = by_tag.get(item.get("source")) if isinstance(item, dict) else None
            if excerpt is None or value is None:
                sources[f["key"]] = None
                continue
            g = excerpt["grounding"]
            if excerpt["tag"] not in numbers:
                numbers[excerpt["tag"]] = len(citations) + 1
                citations.append({
                    "n": numbers[excerpt["tag"]],
                    "document": g.get("document_name"),
                    "location": g.get("location"),
                    "section": g.get("section"),
                    "document_id": g.get("document_id"),
                    "chunk_id": g.get("chunk_id"),
                    "pages": g.get("table_pages"),
                })
            quote = item.get("quote") if isinstance(item.get("quote"), str) and item.get("quote").strip() else None
            sources[f["key"]] = {
                "n": numbers[excerpt["tag"]],
                "document": g.get("document_name"),
                "location": g.get("location"),
                "section": g.get("section"),
                "document_id": g.get("document_id"),
                "chunk_id": g.get("chunk_id"),
                "quote": quote.strip()[:400] if quote else None,
                "quote_verified": bool(quote) and _normalized(quote) in _normalized(excerpt["text"]),
            }
        found = sum(v is not None for v in values.values())
        await ctx.step("result", f"Found {found} of {len(fields)} value(s)")
        await ctx.artifact("extraction.json", data={"values": values, "sources": sources, "citations": citations}, kind="json")

