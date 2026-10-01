"""In-memory chat session state.

Each session owns its own conversation memory and its own set of pending
human-in-the-loop interrupts. Since 2026-09-30, each session also gets its
own MCP client where that's actually possible — over HTTP (servers.json,
start_mcp_servers.ps1) AgentRuntime.acquire_session_client() hands back a
genuinely independent connection/protocol session, so concurrent chats
never multiplex tool calls through one shared client/session object. Over
stdio (servers.docker.json, the Docker image) real per-session isolation
isn't available — every session shares the one already-initialized
subprocess session underneath — so that method instead hands back the
shared client itself (see its docstring in streaming_agent.py for why
pretending otherwise there is actively harmful, not just redundant).
ChatSession.mcp_owns_lifecycle records which case applies for a given
session so SessionManager.delete knows whether it's safe to close it.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from backend.apps.client.memory import ConversationMemory
from backend.apps.api.schemas import InterruptDecisionRequest, QuestionAnswerRequest
from backend.config import settings as app_settings

if TYPE_CHECKING:
    from backend.apps.api.streaming_agent import AgentRuntime, ResilientMcpClient

# Tokens reserved out of num_ctx for the model's own generation on a single
# turn: the visible answer plus, on a thinking model with reasoning_effort
# set (or a model like gpt-oss that reasons by default), its hidden
# chain-of-thought — which shares the same fixed context window as the
# prompt. Without this headroom, a full prompt can leave the model no room
# to think, and Ollama truncates the *prompt* instead to make space, which
# silently drops the oldest turns (see ConversationMemory.max_context_tokens).
_RESERVED_GENERATION_TOKENS = 4096


def _max_context_tokens() -> int | None:
    """None in PROD (real OpenAI enforces its own window and errors clearly
    instead of silently truncating, so message-count pruning is enough)."""
    if app_settings.is_prod:
        return None
    return max(2048, app_settings.ollama.num_ctx - _RESERVED_GENERATION_TOKENS)


@dataclass
class PendingInterrupt:
    interrupt_id: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    future: "asyncio.Future[InterruptDecisionRequest]" = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


@dataclass
class PendingQuestion:
    question_id: str
    call_id: str
    prompt: str
    options: list[str]
    allow_other: bool
    future: "asyncio.Future[QuestionAnswerRequest]" = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


@dataclass
class ChatSession:
    session_id: str
    memory: ConversationMemory
    approval_required_tools: list[str]
    mcp: "Client | ResilientMcpClient"
    """This session's MCP client (see AgentRuntime.acquire_session_client).
    Over HTTP this is a dedicated connection entered by
    SessionManager.create/restore and exited by SessionManager.delete —
    never share it across sessions, that's the exact thing it exists to
    avoid. Over stdio it's the one process-wide shared client; see
    mcp_owns_lifecycle."""
    mcp_owns_lifecycle: bool = True
    """Whether this session owns `mcp`'s connection and must close it on
    delete/eviction (HTTP), vs. it being the shared runtime client that only
    AgentRuntime.close() (app shutdown) may close (stdio). Set from
    AgentRuntime.acquire_session_client's return value."""
    user_id: str = ""
    """Owner of this session — set at creation from the authenticated caller
    (backend.apps.api.auth.get_current_user). Never taken from model/tool
    input: it's what streaming_agent.py force-injects into every tool call
    whose MCP schema declares a user_id parameter (see
    AgentRuntime.user_scoped_tool_names — currently
    retrieval_retrieve_document_v2, retrieval_get_document_info_v2,
    indexing_upload_and_index_document_v2, and their v1 equivalents)
    so the model can never impersonate another user."""
    attached_documents: list[dict[str, Any]] = field(default_factory=list)
    """Documents uploaded into this conversation: [{document_id, filename, content_type}, ...]."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_interrupts: dict[str, PendingInterrupt] = field(default_factory=dict)
    pending_questions: dict[str, PendingQuestion] = field(default_factory=dict)
    last_active: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_active = time.time()

    def add_attached_document(self, document_id: str, filename: str, content_type: str) -> None:
        self.attached_documents.append(
            {"document_id": document_id, "filename": filename, "content_type": content_type}
        )

    def requires_approval(self, tool_name: str) -> bool:
        return "*" in self.approval_required_tools or tool_name in self.approval_required_tools

    def new_interrupt(self, call_id: str, tool_name: str, arguments: dict[str, Any]) -> PendingInterrupt:
        pending = PendingInterrupt(
            interrupt_id=str(uuid.uuid4()),
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        self.pending_interrupts[pending.interrupt_id] = pending
        return pending

    def resolve_interrupt(self, interrupt_id: str, decision: InterruptDecisionRequest) -> bool:
        pending = self.pending_interrupts.get(interrupt_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(decision)
        return True

    def new_question(
        self, call_id: str, prompt: str, options: list[str], allow_other: bool
    ) -> PendingQuestion:
        pending = PendingQuestion(
            question_id=str(uuid.uuid4()),
            call_id=call_id,
            prompt=prompt,
            options=options,
            allow_other=allow_other,
        )
        self.pending_questions[pending.question_id] = pending
        return pending

    def resolve_question(self, question_id: str, answer: QuestionAnswerRequest) -> bool:
        pending = self.pending_questions.get(question_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(answer)
        return True


class SessionManager:
    """Owns every live ChatSession. Not process-shared — one worker only."""

    def __init__(self, idle_timeout_seconds: float, runtime: "AgentRuntime") -> None:
        self._sessions: dict[str, ChatSession] = {}
        self._idle_timeout = idle_timeout_seconds
        self._runtime = runtime

    async def create(
        self,
        system_prompt: str,
        max_history_messages: int,
        approval_required_tools: list[str],
        user_id: str,
        max_tool_result_chars: int = 8000,
    ) -> ChatSession:
        mcp, mcp_owns_lifecycle = await self._runtime.acquire_session_client()
        session = ChatSession(
            session_id=str(uuid.uuid4()),
            memory=ConversationMemory(
                system_prompt, max_history_messages, max_tool_result_chars, _max_context_tokens()
            ),
            approval_required_tools=approval_required_tools,
            mcp=mcp,
            mcp_owns_lifecycle=mcp_owns_lifecycle,
            user_id=user_id,
        )
        self._sessions[session.session_id] = session
        return session

    async def restore(
        self,
        session_id: str,
        system_prompt: str,
        max_history_messages: int,
        approval_required_tools: list[str],
        history: list[dict[str, Any]],
        user_id: str,
        max_tool_result_chars: int = 8000,
    ) -> ChatSession:
        """Rebuild a session that lives in MongoDB but was evicted or lost to a
        restart, replaying its stored turns back into fresh conversation memory
        so the model still has context for the next message."""
        memory = ConversationMemory(
            system_prompt, max_history_messages, max_tool_result_chars, _max_context_tokens()
        )
        for turn in history:
            if turn["role"] == "user":
                memory.add_user(turn["content"])
            elif turn["role"] == "assistant":
                memory.add_assistant(turn["content"])
        mcp, mcp_owns_lifecycle = await self._runtime.acquire_session_client()
        session = ChatSession(
            session_id=session_id,
            memory=memory,
            approval_required_tools=approval_required_tools,
            mcp=mcp,
            mcp_owns_lifecycle=mcp_owns_lifecycle,
            user_id=user_id,
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> ChatSession | None:
        return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            for pending in session.pending_interrupts.values():
                if not pending.future.done():
                    pending.future.cancel()
            for pending_q in session.pending_questions.values():
                if not pending_q.future.done():
                    pending_q.future.cancel()
            if session.mcp_owns_lifecycle:
                # HTTP: this session's own dedicated connection — safe to
                # close without affecting anyone else.
                await session.mcp.__aexit__(None, None, None)
            # stdio: session.mcp IS the process-wide shared client (see
            # AgentRuntime.acquire_session_client) — closing it here would
            # tear down the one MCP connection for every other active
            # session (and happens routinely via evict_idle), so leave it
            # open. Only AgentRuntime.close() (app shutdown) may close it.

    async def evict_idle(self) -> list[str]:
        cutoff = time.time() - self._idle_timeout
        stale = [sid for sid, s in self._sessions.items() if s.last_active < cutoff]
        for sid in stale:
            await self.delete(sid)
        return stale
