"""Postgres-backed persistence for chat conversations (schema `app`, see
backend/migrations/0001_init.sql).

Conversations and their messages are the durable record behind the chat UI:
every session created via POST /api/sessions gets a conversation row, and
every completed turn (the user's message plus the assistant's final
message, including its tool-call steps) is appended to it. This is what the
frontend reads through GET /api/conversations and
GET /api/conversations/{id}/messages, and what lets a conversation survive
an API restart or idle session eviction — routers/chat.py rehydrates an
in-memory ChatSession from here when a session_id isn't found live.

Failures here are logged and swallowed rather than raised: persistence is a
durability layer, not something that should ever break a live chat turn a
user is actively waiting on.

Timestamps go out as epoch seconds (floats), the shape the frontend has
always received.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from backend.core.db import run_sync

logger = logging.getLogger("agentic_api")

DEFAULT_TITLE = "New chat"


class ConversationStore:
    """Repository over app.conversations and app.messages."""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    # --- Conversations ------------------------------------------------------

    async def create_conversation(self, conversation_id: str, user_id: str, title: str = DEFAULT_TITLE) -> None:
        def _do() -> None:
            with self.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO app.conversations (id, user_id, title) VALUES (%s, %s, %s)",
                    (conversation_id, user_id, title),
                )

        try:
            await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to persist new conversation %s", conversation_id)

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        def _do() -> dict[str, Any] | None:
            with self.pool.connection() as conn:
                return conn.execute(
                    "SELECT id, id AS _id, user_id, title,"
                    "       extract(epoch FROM created_at)::float8 AS created_at,"
                    "       extract(epoch FROM updated_at)::float8 AS updated_at"
                    "  FROM app.conversations WHERE id = %s",
                    (conversation_id,),
                ).fetchone()

        try:
            return await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to load conversation %s", conversation_id)
            return None

    async def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        def _do() -> list[dict[str, Any]]:
            with self.pool.connection() as conn:
                return conn.execute(
                    "SELECT id, title,"
                    "       extract(epoch FROM created_at)::float8 AS created_at,"
                    "       extract(epoch FROM updated_at)::float8 AS updated_at"
                    "  FROM app.conversations WHERE user_id = %s ORDER BY updated_at DESC",
                    (user_id,),
                ).fetchall()

        try:
            return await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to list conversations for user %s", user_id)
            return []

    async def set_title_if_default(self, conversation_id: str, title: str) -> None:
        def _do() -> None:
            with self.pool.connection() as conn:
                conn.execute(
                    "UPDATE app.conversations SET title = %s WHERE id = %s AND title = %s",
                    (title[:80], conversation_id, DEFAULT_TITLE),
                )

        try:
            await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to set title for conversation %s", conversation_id)

    async def touch(self, conversation_id: str) -> None:
        def _do() -> None:
            with self.pool.connection() as conn:
                conn.execute("UPDATE app.conversations SET updated_at = now() WHERE id = %s", (conversation_id,))

        try:
            await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to touch conversation %s", conversation_id)

    async def delete_conversation(self, conversation_id: str) -> None:
        def _do() -> None:
            with self.pool.connection() as conn:
                # Messages go with it (ON DELETE CASCADE).
                conn.execute("DELETE FROM app.conversations WHERE id = %s", (conversation_id,))

        try:
            await run_sync(_do)
        except psycopg.Error:
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

        def _do() -> None:
            with self.pool.connection() as conn, conn.transaction():
                conn.execute(
                    "INSERT INTO app.messages (id, conversation_id, role, content, steps)"
                    " VALUES (%s, %s, %s, %s, %s::jsonb)",
                    (message_id, conversation_id, role, content, json.dumps(steps or [], default=str)),
                )
                conn.execute("UPDATE app.conversations SET updated_at = now() WHERE id = %s", (conversation_id,))

        try:
            await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to persist %s message for conversation %s", role, conversation_id)
        return message_id

    async def get_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        def _do() -> list[dict[str, Any]]:
            with self.pool.connection() as conn:
                return conn.execute(
                    "SELECT id, role, content, steps FROM app.messages"
                    " WHERE conversation_id = %s ORDER BY seq",
                    (conversation_id,),
                ).fetchall()

        try:
            return await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to load messages for conversation %s", conversation_id)
            return []
