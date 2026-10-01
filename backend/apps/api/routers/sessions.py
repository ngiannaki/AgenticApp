"""Session lifecycle: create / inspect / delete a chat session."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.apps.api.auth import AuthUser, get_current_user
from backend.apps.api.config import get_api_settings
from backend.apps.api.persistence import DEFAULT_TITLE
from backend.apps.api.schemas import CreateSessionRequest, SessionResponse
from backend.apps.client.config import DEFAULT_SYSTEM_PROMPT

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
logger = logging.getLogger("agentic_api")


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(
    request: Request, body: CreateSessionRequest, user: AuthUser = Depends(get_current_user)
) -> SessionResponse:
    settings = get_api_settings()
    session_manager = request.app.state.session_manager
    runtime = request.app.state.runtime
    store = request.app.state.conversation_store

    approval_required_tools = (
        body.approval_required_tools
        if body.approval_required_tools is not None
        else settings.approval_required_tools
    )

    session = await session_manager.create(
        system_prompt=runtime.system_prompt_for(DEFAULT_SYSTEM_PROMPT),
        max_history_messages=settings.max_history_messages,
        approval_required_tools=approval_required_tools,
        user_id=user.user_id,
        max_tool_result_chars=settings.max_tool_result_chars,
    )

    if store is not None:
        await store.create_conversation(session.session_id, user.user_id, DEFAULT_TITLE)
        logger.info("Persisted new conversation %s for user %s", session.session_id, user.user_id)

    return SessionResponse(session_id=session.session_id, tool_names=runtime.tool_names)


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(session_id: str, request: Request, user: AuthUser = Depends(get_current_user)) -> SessionResponse:
    session = request.app.state.session_manager.get(session_id)
    if session is None or session.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionResponse(session_id=session.session_id, tool_names=request.app.state.runtime.tool_names)


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request, user: AuthUser = Depends(get_current_user)) -> None:
    session = request.app.state.session_manager.get(session_id)
    if session is not None and session.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Session not found")
    store = request.app.state.conversation_store
    if session is None and store is not None:
        # Not live in memory (evicted, or the API restarted): the in-memory
        # check above couldn't run, so ownership has to come from the
        # persisted conversation instead — otherwise any signed-in user could
        # delete anyone's conversation just by knowing its id.
        conversation = await store.get_conversation(session_id)
        if conversation is None or conversation.get("user_id") != user.user_id:
            raise HTTPException(status_code=404, detail="Session not found")
    await request.app.state.session_manager.delete(session_id)
    if store is not None:
        await store.delete_conversation(session_id)
