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
    "found — never guess. Cite the excerpt tag (e.g. E3) each value came from. Output JSON only: "
    '{"<key>": {"value": <any>, "source": "E<n>" | null}}'
)


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
        for f in fields:
            await ctx.step("search", f"Searching for {f['key']}", f.get("query", ""))
            args: dict[str, Any] = {"query": f.get("query") or f.get("description") or f["key"], "top_k": 4}
            if inputs["document_ids"]:
                args["document_ids"] = inputs["document_ids"]
            result = await ctx.call_tool("retrieval_search_my_documents_v2", args)
            for chunk in (result.data or {}).get("source_chunks", []) if result.ok else []:
                grounding = chunk.get("grounding") or {}
                chunk_id = grounding.get("chunk_id") or chunk.get("text", "")[:40]
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                excerpts.append({"tag": f"E{len(excerpts) + 1}", "text": chunk.get("text", "")[:1500], "grounding": grounding})
        if not excerpts:
            raise AgentError("No relevant text was found in your documents.")

        await ctx.step("extract", "Extracting values")
        listing = "\n\n".join(f"[{e['tag']}] ({e['grounding'].get('document_name')}) {e['text']}" for e in excerpts[:40])
        wanted = "\n".join(f"- {f['key']}: {f.get('description', '')}" for f in fields)
        raw = parse_json(await ctx.llm.text(_EXTRACTOR, f"Fields:\n{wanted}\n\nExcerpts:\n{listing}\n\nJSON only."))
        if not isinstance(raw, dict):
            raw = {}

        by_tag = {e["tag"]: e["grounding"] for e in excerpts}
        values: dict[str, Any] = {}
        sources: dict[str, Any] = {}
        for f in fields:
            item = raw.get(f["key"])
            value = item.get("value") if isinstance(item, dict) else item
            values[f["key"]] = value
            g = by_tag.get(item.get("source")) if isinstance(item, dict) else None
            sources[f["key"]] = (
                {"document": g.get("document_name"), "location": g.get("location"), "document_id": g.get("document_id")}
                if g and value is not None else None
            )
        found = sum(v is not None for v in values.values())
        await ctx.step("result", f"Found {found} of {len(fields)} value(s)")
        await ctx.artifact("extraction.json", data={"values": values, "sources": sources}, kind="json")

