"""Read access to durable conversation history (backed by MongoDB).

The frontend uses these to populate the sidebar index and to hydrate a
conversation's messages when the user selects it — session creation and the
chat turn itself write through backend.apps.api.persistence.ConversationStore
directly (see routers/sessions.py and routers/chat.py).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("")
async def list_conversations(request: Request) -> list[dict]:
    store = request.app.state.conversation_store
    if store is None:
        return []
    return await store.list_conversations()


@router.get("/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str, request: Request) -> list[dict]:
    store = request.app.state.conversation_store
    if store is None:
        raise HTTPException(status_code=503, detail="Conversation store is unavailable")
    conversation = await store.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return await store.get_messages(conversation_id)
