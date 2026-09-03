"""Session lifecycle: create / inspect / delete a chat session."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from backend.apps.api.config import get_api_settings
from backend.apps.api.persistence import DEFAULT_TITLE
from backend.apps.api.schemas import CreateSessionRequest, SessionResponse
from backend.apps.client.config import DEFAULT_SYSTEM_PROMPT

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
logger = logging.getLogger("agentic_api")


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(request: Request, body: CreateSessionRequest) -> SessionResponse:
    settings = get_api_settings()
    session_manager = request.app.state.session_manager
    runtime = request.app.state.runtime
    store = request.app.state.conversation_store

    approval_required_tools = (
        body.approval_required_tools
        if body.approval_required_tools is not None
        else settings.approval_required_tools
    )

    session = session_manager.create(
        system_prompt=body.system_prompt or DEFAULT_SYSTEM_PROMPT,
        max_history_messages=settings.max_history_messages,
        approval_required_tools=approval_required_tools,
        max_tool_result_chars=settings.max_tool_result_chars,
    )

    if store is not None:
        await store.create_conversation(session.session_id, DEFAULT_TITLE)
        logger.info("Persisted new conversation %s", session.session_id)

    return SessionResponse(session_id=session.session_id, tool_names=runtime.tool_names)


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(session_id: str, request: Request) -> SessionResponse:
    session = request.app.state.session_manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionResponse(session_id=session.session_id, tool_names=request.app.state.runtime.tool_names)


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request) -> None:
    request.app.state.session_manager.delete(session_id)
    store = request.app.state.conversation_store
    if store is not None:
        await store.delete_conversation(session_id)
