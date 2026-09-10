"""Read access to durable conversation history (backed by MongoDB).

The frontend uses these to populate the sidebar index and to hydrate a
conversation's messages when the user selects it — session creation and the
chat turn itself write through backend.apps.api.persistence.ConversationStore
directly (see routers/sessions.py and routers/chat.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.apps.api.auth import AuthUser, get_current_user

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("")
async def list_conversations(request: Request, user: AuthUser = Depends(get_current_user)) -> list[dict]:
    store = request.app.state.conversation_store
    if store is None:
        return []
    return await store.list_conversations(user.user_id)


@router.get("/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str, request: Request, user: AuthUser = Depends(get_current_user)
) -> list[dict]:
    store = request.app.state.conversation_store
    if store is None:
        raise HTTPException(status_code=503, detail="Conversation store is unavailable")
    conversation = await store.get_conversation(conversation_id)
    if conversation is None or conversation.get("user_id") != user.user_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return await store.get_messages(conversation_id)
