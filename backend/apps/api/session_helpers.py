"""Shared helper for resolving a session that may have been evicted (idle
timeout) or lost (API restart) since the frontend last saw it — used by every
router that operates on an existing session_id (chat turns, file uploads).
"""
from __future__ import annotations

import logging

from fastapi import Request

from backend.apps.api.config import get_api_settings
from backend.apps.api.sessions import ChatSession
from backend.apps.client.config import DEFAULT_SYSTEM_PROMPT

logger = logging.getLogger("agentic_api")


async def rehydrate_session(request: Request, session_id: str, user_id: str) -> ChatSession | None:
    """Try to rebuild an evicted/lost in-memory session from persisted
    conversation history. Returns the restored ChatSession, or None if
    there's no record of it (or it doesn't belong to `user_id`)."""
    store = request.app.state.conversation_store
    if store is None:
        return None

    conversation = await store.get_conversation(session_id)
    if conversation is None or conversation.get("user_id") != user_id:
        return None

    settings = get_api_settings()
    history = await store.get_messages(session_id)
    session = request.app.state.session_manager.restore(
        session_id=session_id,
        system_prompt=request.app.state.runtime.system_prompt_for(DEFAULT_SYSTEM_PROMPT),
        max_history_messages=settings.max_history_messages,
        approval_required_tools=settings.approval_required_tools,
        history=history,
        user_id=user_id,
        max_tool_result_chars=settings.max_tool_result_chars,
    )
    logger.info("Rehydrated session %s from %d persisted message(s)", session_id, len(history))
    return session


async def get_or_rehydrate_session(request: Request, session_id: str, user_id: str) -> ChatSession | None:
    """The common "find this session" path: check the live in-memory map
    first (404-equivalent None if it belongs to someone else), falling back
    to rehydrating it from Mongo."""
    session = request.app.state.session_manager.get(session_id)
    if session is not None:
        return session if session.user_id == user_id else None
    return await rehydrate_session(request, session_id, user_id)
