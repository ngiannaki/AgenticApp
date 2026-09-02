"""Conversation memory management for MCPAgent.

Keeps the running message list (system / user / assistant / tool) that gets
sent to Ollama each turn, and prunes older turns once a soft cap is hit so
long-running chats don't grow the prompt unbounded. Pruning always keeps the
system prompt and the most recent turns intact — only the oldest
user/assistant/tool exchanges are dropped.
"""
from __future__ import annotations

from typing import Any


class ConversationMemory:
    """A pruning message buffer, shaped for ollama.Client.chat(messages=...)."""

    def __init__(self, system_prompt: str, max_messages: int = 40) -> None:
        self.max_messages = max_messages
        self._system: dict[str, Any] = {"role": "system", "content": system_prompt}
        self._messages: list[dict[str, Any]] = []

    def add_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})
        self._prune()

    def add_assistant(self, content: str | None, tool_calls: list[dict] | None = None) -> None:
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self._messages.append(msg)
        self._prune()

    def add_tool_result(self, tool_name: str, content: str) -> None:
        self._messages.append({"role": "tool", "tool_name": tool_name, "content": content})
        self._prune()

    def messages(self) -> list[dict[str, Any]]:
        """Full message list, system prompt first, ready to send to the LLM."""
        return [self._system, *self._messages]

    def clear(self) -> None:
        self._messages.clear()

    def _prune(self) -> None:
        """Drop the oldest turns once over the cap, keeping the tail intact."""
        if len(self._messages) <= self.max_messages:
            return
        overflow = len(self._messages) - self.max_messages
        # Never cut a tool-result message loose from the assistant call that
        # requested it — extend the cut point forward past any tool messages.
        while overflow < len(self._messages) and self._messages[overflow]["role"] == "tool":
            overflow += 1
        self._messages = self._messages[overflow:]
