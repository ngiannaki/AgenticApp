"""Per-user document registry (app.documents in Postgres), backing the "my
files" list and the ownership checks the API enforces (routers/files.py,
and streaming_agent.py's guard on every model tool call naming a
document_id).

Uploading a file (routers/files.py) stores the raw bytes on disk (see
backend.core.storage.document_store.DocumentStore) and indexes it
(backend.mcp_servers.indexing), but this table is the source of truth for
who a document belongs to and what it was originally called.
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from backend.core.db import run_sync

logger = logging.getLogger("agentic_api")

_LIST_COLUMNS = (
    "id AS document_id, original_name, file_type, status, chunks_indexed, error,"
    " extract(epoch FROM created_at)::float8 AS created_at"
)


class DocumentRegistry:
    """Repository over app.documents."""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    async def create(
        self,
        document_id: str,
        user_id: str,
        original_name: str,
        stored_name: str,
        file_type: str,
        status: str,
        chunks_indexed: int,
    ) -> None:
        def _do() -> None:
            with self.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO app.documents"
                    " (id, user_id, original_name, stored_name, file_type, status, chunks_indexed)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (document_id, user_id, original_name, stored_name, file_type, status, chunks_indexed),
                )

        try:
            await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to register document %s for user %s", document_id, user_id)

    async def set_status(
        self, document_id: str, status: str, *, chunks_indexed: int | None = None, error: str | None = None
    ) -> None:
        def _do() -> None:
            with self.pool.connection() as conn:
                conn.execute(
                    "UPDATE app.documents SET status = %s, chunks_indexed = COALESCE(%s, chunks_indexed),"
                    " error = %s, updated_at = now() WHERE id = %s",
                    (status, chunks_indexed, error, document_id),
                )

        try:
            await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to update status of document %s", document_id)

    async def get(self, document_id: str) -> dict[str, Any] | None:
        def _do() -> dict[str, Any] | None:
            with self.pool.connection() as conn:
                return conn.execute(
                    "SELECT id, id AS _id, user_id, original_name, stored_name, file_type, status,"
                    " chunks_indexed, error FROM app.documents WHERE id = %s",
                    (document_id,),
                ).fetchone()

        try:
            return await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to load document %s", document_id)
            return None

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        def _do() -> list[dict[str, Any]]:
            with self.pool.connection() as conn:
                return conn.execute(
                    f"SELECT {_LIST_COLUMNS} FROM app.documents WHERE user_id = %s ORDER BY created_at DESC",
                    (user_id,),
                ).fetchall()

        try:
            return await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to list documents for user %s", user_id)
            return []

    async def with_status(self, status: str) -> list[dict[str, Any]]:
        """Every user's documents in `status` (startup: re-queue "indexing")."""
        def _do() -> list[dict[str, Any]]:
            with self.pool.connection() as conn:
                return conn.execute(
                    "SELECT id, user_id, original_name FROM app.documents WHERE status = %s ORDER BY created_at",
                    (status,),
                ).fetchall()

        return await run_sync(_do)

    async def delete(self, document_id: str, user_id: str) -> bool:
        """Deletes only if `user_id` owns the document. Returns whether it was deleted."""

        def _do() -> bool:
            with self.pool.connection() as conn:
                cur = conn.execute(
                    "DELETE FROM app.documents WHERE id = %s AND user_id = %s", (document_id, user_id)
                )
                return cur.rowcount > 0

        try:
            return await run_sync(_do)
        except psycopg.Error:
            logger.exception("Failed to delete document %s for user %s", document_id, user_id)
            return False

    async def is_owner(self, document_id: str, user_id: str) -> bool:
        doc = await self.get(document_id)
        return doc is not None and doc.get("user_id") == user_id
