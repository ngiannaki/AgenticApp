"""Streaming, interruptible version of backend.apps.client.agent.MCPAgent.

Same tool-calling loop as the CLI agent, but structured as an async
generator of SSE-ready events instead of a single blocking call that
returns the final answer. Before executing a tool call that the session
flags as requiring approval, the generator emits an "interrupt" event and
suspends on a Future until a human resolves it via the resume endpoint —
that's the human-in-the-loop hook.

One AgentRuntime is created once at startup (backend/apps/api/main.py) and
shared read-only across all sessions: it holds the single MCP connection,
the discovered tool list, and the LLM client (Ollama or OpenAI, depending on
ENVIRONMENT — see backend/config). Per-conversation state (memory, pending
interrupts, approval policy) lives on ChatSession instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, AsyncIterator

import openai
from fastmcp import Client

from backend.apps.api import events
from backend.apps.api.config import ApiSettings
from backend.apps.api.sessions import ChatSession
from backend.config import get_async_openai_client, get_ollama_extra_body, settings as app_settings

logger = logging.getLogger("agentic_api")

ASK_USER_TOOL_NAME = "ask_user"

# Tool calls whose user_id argument must come from the authenticated session,
# never from the model. The model is told which document_ids are attached
# (see _attached_documents_note) but is never told or asked for a user_id —
# _handle_tool_call overwrites/injects this field itself right before
# dispatch, so a model can't retrieve or index another user's documents no
# matter what it puts in its own tool-call arguments.
#
# Previously a hardcoded set of tool names, which silently stopped scoping a
# tool the moment its name changed (e.g. a "_v2" tool) or a new user_id-taking
# tool was added and someone forgot to list it here. Now computed from each
# tool's own MCP-advertised input schema instead — see
# AgentRuntime.user_scoped_tool_names — so this is automatic.

# A local tool, not backed by any MCP server: handled entirely inside
# _handle_tool_call by pausing the turn on a PendingQuestion, the same way a
# human-approval interrupt pauses it. Modeled on AskUserQuestion-style
# clarifying questions — a prompt plus optional multiple-choice buttons, with
# free text always accepted as a fallback ("Other").
_ASK_USER_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ASK_USER_TOOL_NAME,
        "description": (
            "Ask the human a clarifying question and wait for their answer before "
            "continuing. Use this when you're blocked on a decision only the user can "
            "make — e.g. which of several ambiguous options they mean, or a choice "
            "with no safe default — rather than guessing. Optionally offer a short "
            "list of choices to show as buttons; the user can still type a free-text "
            "answer instead of picking one. Do not use this for information you could "
            "instead find with another tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The question to ask the user."},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short list of choices to offer as buttons (2-4 recommended). Omit for a free-text-only question.",
                },
            },
            "required": ["prompt"],
        },
    },
}


_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _with_attached_documents_note(session: ChatSession, question: str) -> str:
    """Prepend a short note pointing the model at the right retrieval tools
    for any document_id it needs to work with this turn — both documents
    attached to this session (uploaded from the chat's own attach button)
    and bare document_ids the user typed directly (the expected flow for a
    document uploaded from the standalone Documents page, which isn't tied
    to any session). Only the composed text sent to the model is affected —
    the persisted/displayed chat history keeps the user's original
    `question` (see routers/chat.py, which passes `body.message` to
    persistence separately).

    Tool names here must match the MCP-aggregated names in self.tool_names
    (server key prefix + function name, e.g. "retrieval_retrieve_document_v2"
    for retrieval/server.py's retrieve_document_v2) — not the bare function
    names, which aren't valid tool calls and previously sent the model
    chasing "retrieve_document"/"get_document_info"/"read_document" (all
    404s from _handle_tool_call's unknown-tool check).

    Points at the "_v2" (LangChain hybrid retrieval + reranking) tools,
    matching upload_and_index in routers/files.py, which indexes into the
    same v2 collections — a document attached/uploaded through the API is
    only ever indexed with v2, so retrieval must use v2 too or it won't find
    it. Switch both together if you move back to v1.
    """
    attached_ids = {doc["document_id"] for doc in session.attached_documents}
    mentioned_ids = [m for m in _UUID_RE.findall(question) if m not in attached_ids]

    if not session.attached_documents and not mentioned_ids:
        return question

    lines = []
    if session.attached_documents:
        listing = ", ".join(
            f"{doc['filename']} (document_id={doc['document_id']}, type={doc['content_type']})"
            for doc in session.attached_documents
        )
        lines.append(f"[Attached files: {listing}]")
    if mentioned_ids:
        lines.append(
            "[The message below also mentions this document_id, or these "
            f"document_ids, directly: {', '.join(mentioned_ids)} — treat each as "
            "a document already uploaded and indexed, e.g. via the Documents "
            "page, even though it has no filename here.]"
        )

    note = (
        "\n".join(lines) + "\n"
        "These documents were already uploaded and indexed into the vector "
        "store — there is no local file_path for them, so indexing_read_document "
        "(which needs a server-local file_path) will not work here. To answer "
        "questions about a document's content, call "
        "retrieval_retrieve_document_v2(document_id, query) with a query describing "
        "what you're looking for (call it more than once with different queries "
        "if you need several kinds of information), or "
        "retrieval_get_document_info_v2(document_id) for metadata like section/page "
        "counts."
    )
    return f"{note}\n\n{question}"


def _extract_reasoning(message: Any) -> str | None:
    """Pull the model's chain-of-thought off a chat completion message, if present.

    Ollama's OpenAI-compatible endpoint adds a non-standard "reasoning" field
    to the message for thinking-capable models (e.g. gpt-oss:20b) when
    `reasoning_effort` is set (see get_ollama_extra_body). It isn't part of
    the openai SDK's typed model, so it only surfaces via `model_extra`.
    """
    reasoning = getattr(message, "reasoning", None)
    if not reasoning:
        extra = getattr(message, "model_extra", None) or {}
        reasoning = extra.get("reasoning")
    return reasoning or None


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
    """Shared, process-wide MCP connection + tool list + LLM client."""

    def __init__(self, mcp_client: Client, llm_client: openai.AsyncOpenAI, settings: ApiSettings) -> None:
        self.mcp = mcp_client
        self.llm = llm_client
        self.settings = settings
        self.tools: list[dict[str, Any]] = []

    @classmethod
    async def connect(cls, servers: dict, settings: ApiSettings) -> "AgentRuntime":
        mcp_client = Client(servers)
        await mcp_client.__aenter__()
        llm_client = get_async_openai_client()
        runtime = cls(mcp_client, llm_client, settings)
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
        self.tools.append(_ASK_USER_TOOL_SCHEMA)
        logger.info("Connected to %d MCP tool(s) + ask_user: %s", len(self.tools) - 1, ", ".join(self.tool_names))

    @property
    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.tools]

    @property
    def user_scoped_tool_names(self) -> set[str]:
        """Names of tools whose MCP input schema declares a `user_id`
        parameter — see _USER_SCOPED_TOOLS's comment above for why this is
        derived rather than hardcoded."""
        return {
            t["function"]["name"]
            for t in self.tools
            if "user_id" in ((t["function"]["parameters"] or {}).get("properties") or {})
        }

    async def _chat(
        self, session: ChatSession, tools_enabled: bool = True
    ) -> AsyncIterator[tuple[str, Any]]:
        """Call the chat completions endpoint, recovering from a malformed-tool-call parse failure.

        Some models (observed with gpt-oss:20b) occasionally leak chain-of-thought
        text into the slot where a tool call is expected; Ollama's server tries to
        parse that text as JSON and returns a 500 ("error parsing tool call: raw=...
        err=invalid character ... looking for beginning of value"). That's a
        transient model hiccup, not a fatal error, so nudge the model once or
        twice and retry instead of ending the whole turn.

        Yields ("event", sse_dict) for status/error events to forward as-is, then
        exactly one of ("response", ChatCompletion) on success or ("failed", None)
        after an unrecoverable error (an "error" event has already been yielded).
        """
        max_recovery_attempts = 2
        for attempt in range(max_recovery_attempts + 1):
            try:
                response = await self.llm.chat.completions.create(
                    model=app_settings.chat_model,
                    messages=session.memory.messages(),
                    tools=(self.tools or None) if tools_enabled else None,
                    temperature=self.settings.temperature,
                    extra_body=get_ollama_extra_body(),
                )
                yield ("response", response)
                return
            except openai.APIStatusError as exc:
                malformed_tool_call = "error parsing tool call" in str(exc)
                if malformed_tool_call and attempt < max_recovery_attempts:
                    logger.warning(
                        "Model produced a malformed tool call (attempt %d/%d): %s",
                        attempt + 1,
                        max_recovery_attempts,
                        exc,
                    )
                    yield ("event", events.status("model output could not be parsed as a tool call, retrying"))
                    session.memory.add_user(
                        "Your previous message could not be parsed: reply with either one valid tool "
                        "call, or plain final-answer text — never a mix of prose and partial JSON."
                    )
                    continue
                logger.exception("LLM chat call failed")
                yield ("event", events.error(f"LLM call failed: {exc}"))
                yield ("failed", None)
                return
            except Exception as exc:  # noqa: BLE001 — surface upstream failure and end the turn
                logger.exception("LLM chat call failed")
                yield ("event", events.error(f"LLM call failed: {exc}"))
                yield ("failed", None)
                return

    async def run_stream(self, session: ChatSession, question: str) -> AsyncIterator[dict[str, Any]]:
        """Drive one user turn for `session`, yielding SSE-ready events as it goes."""
        session.touch()
        session.memory.add_user(_with_attached_documents_note(session, question))
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

            message = response.choices[0].message
            reasoning = _extract_reasoning(message)
            if reasoning:
                yield events.reasoning(reasoning)

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
        final_message = response.choices[0].message
        reasoning = _extract_reasoning(final_message)
        if reasoning:
            yield events.reasoning(reasoning)
        final = final_message.content or "Unable to produce a final answer in the allotted iterations."

        session.memory.add_assistant(final)
        yield events.message(final)
        yield events.done(final, max_iterations)

    async def _handle_tool_call(self, session: ChatSession, call: Any) -> AsyncIterator[dict[str, Any]]:
        name = call.function.name
        arguments = json.loads(call.function.arguments) if call.function.arguments else {}
        call_id = str(uuid.uuid4())

        yield events.tool_call(call_id, name, arguments)

        if name == ASK_USER_TOOL_NAME:
            async for event in self._handle_ask_user(session, call, call_id, arguments):
                yield event
            return

        if name not in self.tool_names:
            available = ", ".join(self.tool_names)
            text = (
                f"Error calling tool '{name}': unknown tool. "
                f"Available tools are: {available}. Call one of these exactly, "
                "using the arguments from the conversation or a previous tool result, "
                "or answer directly without a tool call if none of them fit."
            )
            logger.warning("Model called unknown tool '%s' (available: %s)", name, available)
            yield events.tool_result(call_id, name, False, text)
            session.memory.add_tool_result(name, text, tool_call_id=call.id)
            return

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
                session.memory.add_tool_result(name, text, tool_call_id=call.id)
                return
            if decision.action == "edit" and decision.arguments is not None:
                arguments = decision.arguments

        if name in self.user_scoped_tool_names:
            arguments["user_id"] = session.user_id

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
        session.memory.add_tool_result(name, text, tool_call_id=call.id)

    async def _handle_ask_user(
        self, session: ChatSession, call: Any, call_id: str, arguments: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Pause the turn and ask the human a question, the ask_user tool's whole
        job — never dispatched to MCP, since the answer has to come from whoever
        is looking at the frontend, not from a tool server."""
        prompt = str(arguments.get("prompt") or "").strip()
        options = [str(o) for o in (arguments.get("options") or []) if str(o).strip()]

        if not prompt:
            text = "Error calling tool 'ask_user': 'prompt' is required."
            yield events.tool_result(call_id, ASK_USER_TOOL_NAME, False, text)
            session.memory.add_tool_result(ASK_USER_TOOL_NAME, text, tool_call_id=call.id)
            return

        pending = session.new_question(call_id, prompt, options, allow_other=True)
        yield events.question(pending.question_id, call_id, prompt, options, allow_other=True)

        answer = await self._await_answer(pending.future)
        session.pending_questions.pop(pending.question_id, None)

        yield events.tool_result(call_id, ASK_USER_TOOL_NAME, True, answer.answer)
        session.memory.add_tool_result(ASK_USER_TOOL_NAME, answer.answer, tool_call_id=call.id)

    async def _await_answer(self, future: "asyncio.Future") -> Any:
        """Same wait/timeout shape as _await_decision, for a pending question."""
        timeout = self.settings.interrupt_timeout_seconds
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            from backend.apps.api.schemas import QuestionAnswerRequest

            return QuestionAnswerRequest(answer="(no response — timed out waiting for the user)")

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
