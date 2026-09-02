"""In-memory chat session state.

Each session owns its own conversation memory and its own set of pending
human-in-the-loop interrupts. All sessions share one MCP connection and one
tool list (see main.py's lifespan) — sessions are cheap and hold no I/O
resources of their own, so no explicit close/disconnect is needed on top of
letting them expire.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.apps.client.memory import ConversationMemory
from backend.apps.api.schemas import InterruptDecisionRequest


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
class ChatSession:
    session_id: str
    memory: ConversationMemory
    approval_required_tools: list[str]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_interrupts: dict[str, PendingInterrupt] = field(default_factory=dict)
    last_active: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_active = time.time()

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


class SessionManager:
    """Owns every live ChatSession. Not process-shared — one worker only."""

    def __init__(self, idle_timeout_seconds: float) -> None:
        self._sessions: dict[str, ChatSession] = {}
        self._idle_timeout = idle_timeout_seconds

    def create(self, system_prompt: str, max_history_messages: int, approval_required_tools: list[str]) -> ChatSession:
        session = ChatSession(
            session_id=str(uuid.uuid4()),
            memory=ConversationMemory(system_prompt, max_history_messages),
            approval_required_tools=approval_required_tools,
        )
        self._sessions[session.session_id] = session
        return session

    def restore(
        self,
        session_id: str,
        system_prompt: str,
        max_history_messages: int,
        approval_required_tools: list[str],
        history: list[dict[str, Any]],
    ) -> ChatSession:
        """Rebuild a session that lives in MongoDB but was evicted or lost to a
        restart, replaying its stored turns back into fresh conversation memory
        so the model still has context for the next message."""
        memory = ConversationMemory(system_prompt, max_history_messages)
        for turn in history:
            if turn["role"] == "user":
                memory.add_user(turn["content"])
            elif turn["role"] == "assistant":
                memory.add_assistant(turn["content"])
        session = ChatSession(
            session_id=session_id,
            memory=memory,
            approval_required_tools=approval_required_tools,
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> ChatSession | None:
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            for pending in session.pending_interrupts.values():
                if not pending.future.done():
                    pending.future.cancel()

    def evict_idle(self) -> list[str]:
        cutoff = time.time() - self._idle_timeout
        stale = [sid for sid, s in self._sessions.items() if s.last_active < cutoff]
        for sid in stale:
            self.delete(sid)
        return stale
