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
    "When you do have enough real information to satisfy the whole request, "
    "give a direct, concise final answer grounded in the tools' actual results, "
    "without narrating your tool usage."
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
