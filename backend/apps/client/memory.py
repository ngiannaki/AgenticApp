"""Conversation memory management for MCPAgent.

Keeps the running message list (system / user / assistant / tool) that gets
sent to the LLM each turn, and prunes older turns once a soft cap is hit so
long-running chats don't grow the prompt unbounded. Pruning always keeps the
system prompt and the most recent turns intact — only the oldest
user/assistant/tool exchanges are dropped.
"""
from __future__ import annotations

import json
from typing import Any


class ConversationMemory:
    """A pruning message buffer, shaped for an OpenAI-compatible chat.completions.create(messages=...) call."""

    def __init__(
        self,
        system_prompt: str,
        max_messages: int = 40,
        max_tool_result_chars: int = 8000,
        max_context_tokens: int | None = None,
    ) -> None:
        self.max_messages = max_messages
        self.max_tool_result_chars = max_tool_result_chars
        self.max_context_tokens = max_context_tokens
        """Soft cap on the *estimated* token size of the whole message list (system
        prompt included), independent of `max_messages`. A message-count cap alone
        doesn't protect against blowing the model's actual context window: a handful
        of large tool results (or, on a thinking model, generation eating into the
        same fixed window via reasoning tokens) can exceed it long before 40 messages
        are reached. When that happens the *server* (e.g. Ollama, whose num_ctx is
        finite) silently truncates the prompt from the front to make it fit — which
        looks like the conversation "forgetting" the user's original question. Set
        this a bit below the server's context window (num_ctx) to prune from here
        instead, keeping the drop predictable and the tail of the conversation intact."""
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

    def add_tool_result(self, tool_name: str, content: str, tool_call_id: str | None = None) -> None:
        """`tool_call_id` is required for an OpenAI-compatible client to correlate the
        result with the call that requested it; omit it for Ollama's native API."""
        content = self._truncate_tool_result(content)
        if tool_call_id is not None:
            msg: dict[str, Any] = {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        else:
            msg = {"role": "tool", "tool_name": tool_name, "content": content}
        self._messages.append(msg)
        self._prune()

    def messages(self) -> list[dict[str, Any]]:
        """Full message list, system prompt first, ready to send to the LLM."""
        return [self._system, *self._messages]

    def clear(self) -> None:
        self._messages.clear()

    def _truncate_tool_result(self, content: str) -> str:
        limit = self.max_tool_result_chars
        if limit <= 0 or len(content) <= limit:
            return content
        omitted = len(content) - limit
        return f"{content[:limit]}\n\n[... {omitted} more characters truncated — tool results this large should return a URL instead of inlining the content ...]"

    def _prune(self) -> None:
        self._prune_by_count()
        self._prune_by_tokens()

    def _prune_by_count(self) -> None:
        """Drop the oldest turns once over the message-count cap, keeping the tail intact."""
        if len(self._messages) <= self.max_messages:
            return
        self._drop_from_front(len(self._messages) - self.max_messages)

    def _prune_by_tokens(self) -> None:
        """Drop the oldest turns once the estimated prompt size exceeds max_context_tokens."""
        if self.max_context_tokens is None:
            return
        while self._messages and self._estimated_tokens() > self.max_context_tokens:
            if not self._drop_from_front(1):
                break

    def _drop_from_front(self, count: int) -> bool:
        """Cut `count` messages off the front, extended forward past any tool
        message so a tool result is never separated from the assistant call
        that requested it. Returns False if nothing could be cut."""
        cut = count
        while cut < len(self._messages) and self._messages[cut]["role"] == "tool":
            cut += 1
        if cut >= len(self._messages):
            return False
        self._messages = self._messages[cut:]
        return True

    def _estimated_tokens(self) -> int:
        """Rough token count (chars / 4) of the system prompt plus every message,
        good enough for a pruning threshold without pulling in a real tokenizer."""
        total_chars = len(json.dumps(self._system))
        total_chars += sum(len(json.dumps(m)) for m in self._messages)
        return total_chars // 4
