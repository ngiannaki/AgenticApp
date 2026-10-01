"""Deep Research: plan sub-questions, search the user's documents (and
optionally the web) over a few rounds, then write a cited report."""
from __future__ import annotations

import json
from typing import Any

from backend.apps.agents.base import AgentError, BaseAgent, Field, RunContext
from backend.core.citations import _collect_and_stamp_citations, _render_citations

_DEPTH = {  # sub-questions, rounds, chunks per search
    "quick": (3, 1, 5),
    "standard": (4, 2, 6),
    "deep": (6, 3, 8),
}
_EVIDENCE_CHARS = 24000

_PLANNER = (
    "You plan research. Break the user's question into focused sub-questions that, answered together, "
    "fully answer it. If the question is genuinely ambiguous in a way that changes what to research, set "
    '"clarify" to ONE short question for the user; otherwise null. JSON: {"clarify": string|null, "sub_questions": [string]}'
)
_REVIEWER = (
    "You review research coverage. Given the question and the evidence gathered so far, list up to 3 follow-up "
    'search queries for important gaps, or an empty list if the evidence is enough. JSON: {"follow_up": [string]}'
)
_WRITER = (
    "You write a well-structured research report in markdown: a short summary first, then sections. Use ONLY "
    "the evidence provided. After every claim taken from evidence, copy that evidence's tag exactly, e.g. "
    '<citation id="abc"/>. Never invent tags. If the evidence doesn\'t cover something, say so.'
)


class DeepResearchAgent(BaseAgent):
    id = "deep_research"
    name = "Deep Research"
    description = (
        "Researches a question across your documents (and optionally the web) in several rounds "
        "and writes a cited report."
    )
    inputs = [
        Field("question", "textarea", "Research question", required=True),
        Field("document_ids", "documents", "Documents (leave empty for all of yours)"),
        Field("use_web", "boolean", "Also search the web", default=True),
        Field("depth", "select", "Depth", default="standard", options=["quick", "standard", "deep"]),
    ]
    allowed_tools = {"retrieval_search_my_documents_v2", "retrieval_web_search", "retrieval_fetch_page"}
    examples = ["Compare what my reports say about revenue growth with current market data"]

    async def run(self, ctx: RunContext, inputs: dict[str, Any]) -> None:
        question = inputs["question"]
        n_sub, rounds, top_k = _DEPTH.get(inputs["depth"], _DEPTH["standard"])
        citations: list[dict] = []
        chunk_numbers: dict[str, int] = {}
        evidence: list[str] = []

        await ctx.step("plan", "Planning the research")
        plan = await ctx.llm.json(_PLANNER, f"Question: {question}\nAt most {n_sub} sub-questions.")
        if isinstance(plan, dict) and plan.get("clarify"):
            answer = await ctx.ask(str(plan["clarify"]))
            question = f"{question}\n(Clarification: {answer})"
            plan = await ctx.llm.json(_PLANNER + " Do not ask for clarification again.", f"Question: {question}\nAt most {n_sub} sub-questions.")
        queries = [str(q) for q in (plan or {}).get("sub_questions", [])][:n_sub] or [question]
        await ctx.step("plan", f"{len(queries)} sub-questions", "\n".join(f"- {q}" for q in queries))

        for round_no in range(1, rounds + 1):
            for i, query in enumerate(queries, start=1):
                await ctx.step("search", f"Round {round_no}: {query}", f"{i}/{len(queries)}")
                evidence += await self._search_documents(ctx, query, inputs["document_ids"], top_k, citations, chunk_numbers)
                if inputs["use_web"]:
                    evidence += await self._search_web(ctx, query, citations, chunk_numbers)
            if round_no == rounds:
                break
            review = await ctx.llm.json(_REVIEWER, f"Question: {question}\n\nEvidence:\n{self._clip(evidence)}")
            queries = [str(q) for q in (review or {}).get("follow_up", [])][:3]
            if not queries:
                break
            await ctx.step("plan", "Following up on gaps", "\n".join(f"- {q}" for q in queries))

        if not evidence:
            raise AgentError("No relevant evidence was found in your documents" + (" or on the web." if inputs["use_web"] else "."))

        await ctx.step("write", "Writing the report")
        draft = await ctx.llm.text(_WRITER, f"Question: {question}\n\nEvidence:\n{self._clip(evidence)}")
        report, cited = _render_citations(draft, citations)
        sources = [
            {"number": c["number"], "title": c["grounding"].get("document_name") or c["grounding"].get("source"),
             "source": c["grounding"].get("source"), "location": c["grounding"].get("location"), "text": c["text"][:500]}
            for c in cited
        ]
        if sources:
            report += "\n\n## Sources\n" + "\n".join(
                f"{s['number']}. {s['title']}" + (f" (p. {s['location']})" if s.get("location") else "")
                + (f" — {s['source']}" if str(s.get("source", "")).startswith("http") else "")
                for s in sources
            )
        await ctx.artifact("report.md", text=report)
        await ctx.artifact("sources", data=sources, kind="sources")

    async def _search_documents(self, ctx, query, document_ids, top_k, citations, chunk_numbers) -> list[str]:
        args: dict[str, Any] = {"query": query, "top_k": top_k}
        if document_ids:
            args["document_ids"] = document_ids
        result = await ctx.call_tool("retrieval_search_my_documents_v2", args)
        if not result.ok:
            await ctx.step("note", "Document search failed", result.text[:300])
            return []
        # Same stamping as chat (citations are keyed off retrieve_document_v2 results).
        stamped = json.loads(_collect_and_stamp_citations(citations, chunk_numbers, "retrieve_document_v2", True, result.text))
        chunks = stamped.get("source_chunks") or []
        return [f"{c.get('cite_as', '')} [{(c.get('grounding') or {}).get('document_name')}] {c.get('text', '')[:1500]}" for c in chunks]

    async def _search_web(self, ctx, query, citations, chunk_numbers) -> list[str]:
        found = await ctx.call_tool("retrieval_web_search", {"query": query, "max_results": 3})
        results = (found.data or {}).get("results") or [] if found.ok else []
        out = []
        for hit in results[:2]:
            url = hit.get("url")
            page = await ctx.call_tool("retrieval_fetch_page", {"url": url})
            if not page.ok or not (page.data or {}).get("success"):
                continue
            await ctx.step("read", f"Read {hit.get('title') or url}", url)
            chunk_id = f"web-{len(citations) + 1}"
            chunk_numbers[chunk_id] = len(citations) + 1
            text = (page.data.get("content") or "")[:3000]
            citations.append({"number": len(citations) + 1, "text": text,
                              "grounding": {"chunk_id": chunk_id, "document_name": hit.get("title") or url, "source": url}})
            out.append(f'<citation id="{chunk_id}"/> [{hit.get("title")}] {text}')
        return out

    @staticmethod
    def _clip(evidence: list[str]) -> str:
        out, total = [], 0
        for item in evidence:
            if total + len(item) > _EVIDENCE_CHARS:
                break
            out.append(item)
            total += len(item)
        return "\n\n".join(out)
