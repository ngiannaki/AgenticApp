"""MongoDB-backed persistence for chat conversations.

Conversations and their messages are the durable record behind the chat UI:
every session created via POST /api/sessions gets a conversation document,
and every completed turn (the user's message plus the assistant's final
message, including its tool-call steps) is appended to it. This is what the
frontend reads through GET /api/conversations and
GET /api/conversations/{id}/messages, and what lets a conversation survive
an API restart or idle session eviction — routers/chat.py rehydrates an
in-memory ChatSession from here when a session_id isn't found live.

Failures here are logged and swallowed rather than raised: persistence is a
durability layer, not something that should ever break a live chat turn a
user is actively waiting on.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import PyMongoError

logger = logging.getLogger("agentic_api")

DEFAULT_TITLE = "New chat"


class ConversationStore:
    """Async repository over the `conversations` and `messages` collections."""

    def __init__(self, client: AsyncIOMotorClient, db: AsyncIOMotorDatabase) -> None:
        self.client = client
        self._conversations = db["conversations"]
        self._messages = db["messages"]

    @classmethod
    async def connect(cls, mongo_url: str, db_name: str) -> "ConversationStore":
        client: AsyncIOMotorClient = AsyncIOMotorClient(
            mongo_url, serverSelectionTimeoutMS=5000
        )
        await client.admin.command("ping")
        store = cls(client, client[db_name])
        await store._ensure_indexes()
        logger.info("Connected to MongoDB for conversation persistence (db=%s)", db_name)
        return store

    async def close(self) -> None:
        self.client.close()

    async def _ensure_indexes(self) -> None:
        await self._conversations.create_index("updated_at")
        await self._conversations.create_index("user_id")
        await self._messages.create_index([("conversation_id", 1), ("created_at", 1)])

    # --- Conversations ------------------------------------------------------

    async def create_conversation(self, conversation_id: str, user_id: str, title: str = DEFAULT_TITLE) -> None:
        now = time.time()
        try:
            await self._conversations.insert_one(
                {"_id": conversation_id, "user_id": user_id, "title": title, "created_at": now, "updated_at": now}
            )
        except PyMongoError:
            logger.exception("Failed to persist new conversation %s", conversation_id)

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        try:
            return await self._conversations.find_one({"_id": conversation_id})
        except PyMongoError:
            logger.exception("Failed to load conversation %s", conversation_id)
            return None

    async def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        try:
            cursor = self._conversations.find({"user_id": user_id}).sort("updated_at", -1)
            return [
                {
                    "id": doc["_id"],
                    "title": doc.get("title", DEFAULT_TITLE),
                    "created_at": doc.get("created_at"),
                    "updated_at": doc.get("updated_at"),
                }
                async for doc in cursor
            ]
        except PyMongoError:
            logger.exception("Failed to list conversations for user %s", user_id)
            return []

    async def set_title_if_default(self, conversation_id: str, title: str) -> None:
        try:
            await self._conversations.update_one(
                {"_id": conversation_id, "title": DEFAULT_TITLE},
                {"$set": {"title": title[:80]}},
            )
        except PyMongoError:
            logger.exception("Failed to set title for conversation %s", conversation_id)

    async def touch(self, conversation_id: str) -> None:
        try:
            await self._conversations.update_one(
                {"_id": conversation_id}, {"$set": {"updated_at": time.time()}}
            )
        except PyMongoError:
            logger.exception("Failed to touch conversation %s", conversation_id)

    async def delete_conversation(self, conversation_id: str) -> None:
        try:
            await self._conversations.delete_one({"_id": conversation_id})
            await self._messages.delete_many({"conversation_id": conversation_id})
        except PyMongoError:
            logger.exception("Failed to delete conversation %s", conversation_id)

    # --- Messages -------------------------------------------------------------

    async def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        steps: list[dict[str, Any]] | None = None,
    ) -> str:
        message_id = str(uuid.uuid4())
        now = time.time()
        try:
            await self._messages.insert_one(
                {
                    "_id": message_id,
                    "conversation_id": conversation_id,
                    "role": role,
                    "content": content,
                    "steps": steps or [],
                    "created_at": now,
                }
            )
            await self._conversations.update_one(
                {"_id": conversation_id}, {"$set": {"updated_at": now}}
            )
        except PyMongoError:
            logger.exception(
                "Failed to persist %s message for conversation %s", role, conversation_id
            )
        return message_id

    async def get_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        try:
            cursor = self._messages.find({"conversation_id": conversation_id}).sort("created_at", 1)
            return [
                {
                    "id": doc["_id"],
                    "role": doc["role"],
                    "content": doc["content"],
                    "steps": doc.get("steps", []),
                }
                async for doc in cursor
            ]
        except PyMongoError:
            logger.exception("Failed to load messages for conversation %s", conversation_id)
            return []
