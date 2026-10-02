"""The runner behind every user-built agent. The API resolves the user's
agent (instructions + skills -> allowed tools) and passes it as the
`_definition` input; the user's own `request` and documents come with it.
One generic tool loop: the model only sees the tools the skills grant, and
anything that changes data pauses for the user's approval."""
from __future__ import annotations

import json
from typing import Any

from backend.apps.agents.base import AgentError, BaseAgent, Field, RunContext
from backend.config import settings
from backend.core.agent_tools import ALL_GROUP_TOOLS

_FRAME = (
    "You are {name}, an assistant the user configured for a specific job.\n\n"
    "{instructions}\n\n{skills}"
    "How to work: use the tools available to you to find real information before answering — never invent "
    "facts, numbers or sources. When you use the user's documents, cite them inline as [document name, p. N]. "
    "Files you create (reports) and interactive pages you build are shown to the user automatically next to "
    "your answer — mention what you made in a sentence, don't paste raw URLs. "
    "Reply in clear markdown (headings, lists, tables where useful)."
)


class CustomAgent(BaseAgent):
    id = "custom"
    name = "Custom agent"
    description = "Runs an agent you built (internal: started by the API with its definition)."
    inputs = [
        Field("request", "textarea", "What should the agent do?", required=True),
        Field("document_ids", "documents", "Documents (optional)"),
    ]
    allowed_tools = ALL_GROUP_TOOLS

    def validate(self, inputs: dict[str, Any]) -> dict[str, Any]:
        clean = super().validate(inputs)
        definition = inputs.get("_definition")
        if not isinstance(definition, dict) or not definition.get("name"):
            raise AgentError("This agent's definition is missing — start it from the Agents page.")
        clean["_definition"] = definition
        return clean

    async def run(self, ctx: RunContext, inputs: dict[str, Any]) -> None:
        d = inputs["_definition"]
        tools = set(d.get("tools") or []) & self.allowed_tools
        skills = "".join(
            f"## Skill: {s.get('name')}\n{(s.get('instructions') or '').strip()}\n\n" for s in d.get("skills") or []
        )
        system = _FRAME.format(
            name=d["name"], instructions=(d.get("instructions") or "").strip() or "Help the user with their request.", skills=skills
        )
        docs = inputs["document_ids"]
        user = inputs["request"] + (f"\n\nDocuments the user selected (document_ids): {', '.join(docs)}" if docs else "")
        files: list[dict] = []

        async def on_call(name: str, args: dict, ok: bool, text: str) -> None:
            data = _json(text)
            produced = [a for a in (data or {}).get("artifacts", []) if a.get("url")] if isinstance(data, dict) else []
            # generate_file / generate_ui return a single file at the top level.
            if isinstance(data, dict) and ok and data.get("url") and data.get("filename"):
                produced.append({k: data.get(k) for k in ("url", "filename", "content_type", "size_bytes")})
            files.extend(produced)
            detail = args.get("code") or args.get("sql") or args.get("query") or args.get("html") or json.dumps(args)[:500]
            output = ""
            if isinstance(data, dict):
                output = str(data.get("stdout") or data.get("stderr") or data.get("error") or "")[:4000]
            kind = "code" if "code" in args or "html" in args else "search" if "query" in args else "write" if name.endswith("generate_file") else "tool"
            await ctx.step(kind, f"{_label(name)}{'' if ok else ' (failed)'}", str(detail), {"output": output, "files": produced})

        await ctx.step("plan", f"{d['name']} is working on it", f"Tools: {', '.join(sorted(_label(t) for t in tools)) or 'none'}")
        answer = await ctx.tool_loop(
            system, user, tools, on_call=on_call,
            confirm=tools & set(settings.api.mandatory_approval_tools),
        )
        await ctx.artifact("result.md", text=answer or "The agent produced no answer.")
        if files:
            await ctx.artifact("files", data=files, kind="files")


def _label(tool: str) -> str:
    return tool.split("_", 1)[-1].replace("_v2", "").replace("_", " ")


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None
