"""Streaming, interruptible version of backend.apps.client.agent.MCPAgent.

Same tool-calling loop as the CLI agent, but structured as an async
generator of SSE-ready events instead of a single blocking call that
returns the final answer. Before executing a tool call that the session
flags as requiring approval, the generator emits an "interrupt" event and
suspends on a Future until a human resolves it via the resume endpoint —
that's the human-in-the-loop hook.

One AgentRuntime is created once at startup (backend/apps/api/main.py) and
shared read-only across all sessions: it holds the single MCP connection,
the discovered tool list, and the Ollama client. Per-conversation state
(memory, pending interrupts, approval policy) lives on ChatSession instead.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncIterator

import ollama
from fastmcp import Client

from backend.apps.api import events
from backend.apps.api.config import ApiSettings
from backend.apps.api.sessions import ChatSession
from backend.core.config import settings as ollama_settings

logger = logging.getLogger("agentic_api")


def _extract_text(result: Any) -> str:
    """Mirror MCPAgent._execute_tool_call's result-unwrapping for fastmcp CallToolResult."""
    if isinstance(result, str):
        return result
    content = getattr(result, "content", result)
    if isinstance(content, list):
        text = "\n".join(b.text for b in content if hasattr(b, "text"))
    else:
        text = str(content) if content else str(result)
    return text or f"Tool returned no content. Raw result: {str(result)[:200]}"


class AgentRuntime:
    """Shared, process-wide MCP connection + tool list + Ollama client."""

    def __init__(self, mcp_client: Client, ollama_client: ollama.AsyncClient, settings: ApiSettings) -> None:
        self.mcp = mcp_client
        self.ollama = ollama_client
        self.settings = settings
        self.tools: list[dict[str, Any]] = []

    @classmethod
    async def connect(cls, servers: dict, settings: ApiSettings) -> "AgentRuntime":
        mcp_client = Client(servers)
        await mcp_client.__aenter__()
        ollama_client = ollama.AsyncClient(host=ollama_settings.base_url)
        runtime = cls(mcp_client, ollama_client, settings)
        await runtime._load_tools()
        return runtime

    async def close(self) -> None:
        await self.mcp.__aexit__(None, None, None)

    async def _load_tools(self) -> None:
        mcp_tools = await self.mcp.list_tools()
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema,
                },
            }
            for t in mcp_tools
        ]
        logger.info("Connected to %d MCP tool(s): %s", len(self.tools), ", ".join(self.tool_names))

    @property
    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.tools]

    async def _chat(
        self, session: ChatSession, tools_enabled: bool = True
    ) -> AsyncIterator[tuple[str, Any]]:
        """Call ollama.chat, recovering from a malformed-tool-call parse failure.

        Some models (observed with gpt-oss:20b) occasionally leak chain-of-thought
        text into the slot where a tool call is expected; Ollama's server tries to
        parse that text as JSON and returns a 500 ("error parsing tool call: raw=...
        err=invalid character ... looking for beginning of value"). That's a
        transient model hiccup, not a fatal error, so nudge the model once or
        twice and retry instead of ending the whole turn.

        Yields ("event", sse_dict) for status/error events to forward as-is, then
        exactly one of ("response", ChatResponse) on success or ("failed", None)
        after an unrecoverable error (an "error" event has already been yielded).
        """
        max_recovery_attempts = 2
        for attempt in range(max_recovery_attempts + 1):
            try:
                response = await self.ollama.chat(
                    model=ollama_settings.chat_model,
                    messages=session.memory.messages(),
                    tools=(self.tools or None) if tools_enabled else None,
                    options={"temperature": self.settings.temperature},
                )
                yield ("response", response)
                return
            except ollama.ResponseError as exc:
                malformed_tool_call = "error parsing tool call" in exc.error
                if malformed_tool_call and attempt < max_recovery_attempts:
                    logger.warning(
                        "Model produced a malformed tool call (attempt %d/%d): %s",
                        attempt + 1,
                        max_recovery_attempts,
                        exc.error,
                    )
                    yield ("event", events.status("model output could not be parsed as a tool call, retrying"))
                    session.memory.add_user(
                        "Your previous message could not be parsed: reply with either one valid tool "
                        "call, or plain final-answer text — never a mix of prose and partial JSON."
                    )
                    continue
                logger.exception("Ollama chat call failed")
                yield ("event", events.error(f"LLM call failed: {exc}"))
                yield ("failed", None)
                return
            except Exception as exc:  # noqa: BLE001 — surface upstream failure and end the turn
                logger.exception("Ollama chat call failed")
                yield ("event", events.error(f"LLM call failed: {exc}"))
                yield ("failed", None)
                return

    async def run_stream(self, session: ChatSession, question: str) -> AsyncIterator[dict[str, Any]]:
        """Drive one user turn for `session`, yielding SSE-ready events as it goes."""
        session.touch()
        session.memory.add_user(question)
        max_iterations = self.settings.max_iterations

        for iteration in range(1, max_iterations + 1):
            session.touch()
            yield events.status(f"iteration {iteration}/{max_iterations}")

            response = None
            async for kind, payload in self._chat(session, tools_enabled=True):
                if kind == "event":
                    yield payload
                elif kind == "response":
                    response = payload
                elif kind == "failed":
                    return
            if response is None:
                return

            message = response.message

            if not message.tool_calls:
                answer = message.content or ""
                session.memory.add_assistant(answer)
                if not answer and iteration < max_iterations:
                    yield events.status("empty response from model, retrying")
                    continue
                yield events.message(answer)
                yield events.done(answer, iteration)
                return

            session.memory.add_assistant(
                message.content,
                tool_calls=[tc.model_dump() for tc in message.tool_calls],
            )

            for call in message.tool_calls:
                async for event in self._handle_tool_call(session, call):
                    yield event

        # Iteration budget exhausted: ask once more, tools disabled, to force an answer.
        yield events.status("max iterations reached, forcing final answer")
        response = None
        async for kind, payload in self._chat(session, tools_enabled=False):
            if kind == "event":
                yield payload
            elif kind == "response":
                response = payload
            elif kind == "failed":
                return
        if response is None:
            return
        final = response.message.content or "Unable to produce a final answer in the allotted iterations."

        session.memory.add_assistant(final)
        yield events.message(final)
        yield events.done(final, max_iterations)

    async def _handle_tool_call(self, session: ChatSession, call: Any) -> AsyncIterator[dict[str, Any]]:
        name = call.function.name
        arguments = dict(call.function.arguments or {})
        call_id = str(uuid.uuid4())

        yield events.tool_call(call_id, name, arguments)

        if session.requires_approval(name):
            pending = session.new_interrupt(call_id, name, arguments)
            yield events.interrupt(pending.interrupt_id, call_id, name, arguments)

            decision = await self._await_decision(pending.future)
            session.pending_interrupts.pop(pending.interrupt_id, None)

            if decision.action == "reject":
                text = f"Tool call '{name}' was rejected by the user."
                if decision.reason:
                    text += f" Reason: {decision.reason}"
                yield events.tool_result(call_id, name, False, text)
                session.memory.add_tool_result(name, text)
                return
            if decision.action == "edit" and decision.arguments is not None:
                arguments = decision.arguments

        yield events.status(f"calling {name}")
        try:
            result = await self.mcp.call_tool(name, arguments)
            text = _extract_text(result)
            ok = True
        except Exception as exc:  # noqa: BLE001 — surface any tool failure back to the model
            text = f"Error calling tool '{name}': {exc}"
            ok = False
            logger.error(text)

        yield events.tool_result(call_id, name, ok, text)
        session.memory.add_tool_result(name, text)

    async def _await_decision(self, future: "asyncio.Future") -> Any:
        """Wait on a pending interrupt.

        `EventSourceResponse`'s built-in ping loop (routers/chat.py) keeps the
        connection alive independently of this coroutine, so all that's needed
        here is to respect the configured decision timeout, if any.
        """
        timeout = self.settings.interrupt_timeout_seconds
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            from backend.apps.api.schemas import InterruptDecisionRequest

            return InterruptDecisionRequest(action="reject", reason="Timed out waiting for human decision.")
