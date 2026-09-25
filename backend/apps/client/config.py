"""
Configuration for the generic MCP agent client.

Server connectivity lives in a JSON mapping file (default: client/servers.json,
same 'mcpServers' shape FastMCP's Client accepts natively). Model / behavior
knobs come from .env (via config.settings) or can be overridden per-instance.

This is the file you edit per-app: swap the system prompt, point at a
different servers.json, or tune max_iterations / temperature.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from backend.config import settings
DEFAULT_SERVERS_PATH = Path(__file__).parent / "servers.json"

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant with access to tools. "
    "Only call a tool when it is actually needed to answer the user's current "
    "message — never call a tool speculatively, to demonstrate it, or just "
    "because it is available. "
    "Every argument you pass to a tool must come from the conversation or "
    "from a previous tool result; never invent, guess, or use placeholder "
    "values (e.g. 'your_x_here') for an argument. "
    "If a tool needs information the user hasn't given you yet, ask the user "
    "for it instead of calling the tool with a made-up value. "
    "If the user's message is empty, unclear, or not an actionable request, "
    "ask a clarifying question instead of calling a tool. "
    "A single request often needs more than one tool call before you can answer "
    "— e.g. retrieving data is not the end goal if the user also asked you to "
    "chart, summarize, or save it; that follow-up step is part of the same "
    "request, not a separate one. After a tool result, decide whether it fully "
    "satisfies everything the user asked for; if not, call the next tool that "
    "gets you there instead of answering early. "
    "A tool call returning without an error is not the same as it having done "
    "what you intended — check the actual result (the values, row counts, file "
    "contents, output) before reporting success; if the result doesn't show what "
    "you expected, say so or try something different instead of assuming it worked. "
    "When you do have enough real information to satisfy the whole request, "
    "give a direct, concise final answer grounded in the tools' actual results. "
    "Do not present a specific number, row, fact, or outcome as if a tool "
    "produced it unless you actually saw it in a tool result, even one that "
    "sounds plausible — general knowledge or your own reasoning is fine when "
    "no tool applies, just don't dress it up as tool-derived. Answer without "
    "narrating your tool usage. "
    "When you answer using chunks returned by a retrieve_document_v2-style tool, "
    "each chunk in the result includes a field named cite_as, e.g. "
    "<citation id=\"...\"/> — right after each claim that comes from that chunk, "
    "copy that exact tag, character-for-character, from that same chunk. Never "
    "write a citation tag from memory, guess one, edit the id inside it, or reuse "
    "one chunk's tag for a claim that actually came from a different chunk — copy "
    "each tag only from the specific chunk you are citing at that moment, even if "
    "you have seen many chunks this turn. Skip the tag entirely for anything not "
    "grounded in a retrieved chunk (e.g. your own reasoning or general knowledge)."
)


class AgentConfig(BaseModel):
    """All knobs for MCPAgent. Construct directly or via `AgentConfig.load()`."""

    servers: dict = Field(default_factory=dict)
    """FastMCP-style {'mcpServers': {...}} mapping."""

    # This CLI talks to Ollama's native API directly (ollama.Client in agent.py),
    # so it always uses the DEV model regardless of ENVIRONMENT.
    ollama_base_url: str = settings.ollama.base_url
    ollama_model: str = settings.chat.dev

    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_iterations: int = 15
    """Max tool-call round-trips per query before the agent is forced to answer."""

    temperature: float = 0.3

    max_history_messages: int = 40
    """Soft cap on retained conversation turns before older ones are pruned."""

    log_level: str = "INFO"

    @classmethod
    def load(
        cls,
        servers_path: str | Path = DEFAULT_SERVERS_PATH,
        **overrides,
    ) -> "AgentConfig":
        """Load server mapping from a JSON file and apply any field overrides."""
        path = Path(servers_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        servers = raw.get("mcpServers", raw)
        # Strip helper/comment keys (leading underscore) some example files carry.
        servers = {k: v for k, v in servers.items() if not k.startswith("_")}
        return cls(servers={"mcpServers": servers}, **overrides)
