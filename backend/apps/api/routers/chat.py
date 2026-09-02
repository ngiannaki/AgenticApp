"""Streaming chat turn (SSE) for a session.

Every turn is written through to MongoDB (backend.apps.api.persistence) as it
streams: the user's message goes in immediately, and the assistant's message
— its final text plus every status/tool_call/tool_result/interrupt/error step
along the way, in the same shape the frontend renders — goes in once the turn
ends. If the in-memory session was evicted (idle timeout) or lost (API
restart) but its conversation still exists in Mongo, it's transparently
rebuilt from history before the turn runs.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from backend.apps.api import events
from backend.apps.api.config import get_api_settings
from backend.apps.api.schemas import ChatRequest
from backend.apps.client.config import DEFAULT_SYSTEM_PROMPT

router = APIRouter(prefix="/api/sessions", tags=["chat"])
logger = logging.getLogger("agentic_api")


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    return json.loads(event["data"])


def _to_step(event: dict[str, Any]) -> dict[str, Any] | None:
    """Map one SSE event to the step shape the frontend's AgentStep expects,
    so persisted history renders identically to a live turn."""
    kind = event["event"]
    data = _event_data(event)
    step_id = str(uuid.uuid4())

    if kind == "status":
        return {"id": step_id, "type": "status", "message": data.get("message")}
    if kind == "tool_call":
        return {
            "id": step_id,
            "type": "tool_call",
            "callId": data.get("call_id"),
            "name": data.get("name"),
            "arguments": data.get("arguments"),
        }
    if kind == "interrupt":
        return {
            "id": step_id,
            "type": "interrupt",
            "interruptId": data.get("interrupt_id"),
            "callId": data.get("call_id"),
            "name": data.get("name"),
            "arguments": data.get("arguments"),
        }
    if kind == "tool_result":
        return {
            "id": step_id,
            "type": "tool_result",
            "callId": data.get("call_id"),
            "name": data.get("name"),
            "ok": data.get("ok"),
            "result": data.get("result"),
        }
    if kind == "error":
        return {"id": step_id, "type": "error", "detail": data.get("detail")}
    return None


async def _rehydrate_session(request: Request, session_id: str) -> Any:
    """Try to rebuild an evicted/lost in-memory session from persisted history.
    Returns the restored ChatSession, or None if there's no record of it."""
    store = request.app.state.conversation_store
    if store is None:
        return None

    conversation = await store.get_conversation(session_id)
    if conversation is None:
        return None

    settings = get_api_settings()
    history = await store.get_messages(session_id)
    session = request.app.state.session_manager.restore(
        session_id=session_id,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        max_history_messages=settings.max_history_messages,
        approval_required_tools=settings.approval_required_tools,
        history=history,
    )
    logger.info("Rehydrated session %s from %d persisted message(s)", session_id, len(history))
    return session


@router.post("/{session_id}/messages")
async def send_message(session_id: str, body: ChatRequest, request: Request) -> EventSourceResponse:
    session_manager = request.app.state.session_manager
    runtime = request.app.state.runtime
    store = request.app.state.conversation_store
    settings = get_api_settings()

    session = session_manager.get(session_id)
    if session is None:
        session = await _rehydrate_session(request, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.lock.locked():
        raise HTTPException(status_code=409, detail="A turn is already in progress for this session")

    async def stream() -> AsyncIterator[dict]:
        steps: list[dict[str, Any]] = []
        final_content = ""

        async with session.lock:
            if store is not None:
                await store.append_message(session_id, "user", body.message)
                await store.set_title_if_default(session_id, body.message)

            try:
                async for event in runtime.run_stream(session, body.message):
                    step = _to_step(event)
                    if step is not None:
                        steps.append(step)
                    if event["event"] == "message":
                        final_content = _event_data(event).get("content", "")
                    elif event["event"] == "done":
                        final_content = final_content or _event_data(event).get("answer", "")
                    yield event
            except Exception as exc:  # noqa: BLE001 — never let the SSE stream die silently
                logger.exception("Unhandled error while streaming session %s", session_id)
                err = events.error(str(exc))
                step = _to_step(err)
                if step is not None:
                    steps.append(step)
                final_content = final_content or f"Error: {exc}"
                yield err
            finally:
                if store is not None:
                    await store.append_message(session_id, "assistant", final_content, steps)

    return EventSourceResponse(stream(), ping=int(settings.interrupt_heartbeat_seconds))
