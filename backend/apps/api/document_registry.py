"""Per-user document registry (MongoDB), backing the "my files" list and the
ownership check retrieval enforces.

Uploading a file (routers/files.py) stores the raw bytes on disk (see
backend.core.storage.document_store.DocumentStore) and indexes it into the
vector store (backend.mcp_servers.indexing), but neither of those layers know
who a document belongs to or what it was originally called — that's what this
collection is the source of truth for.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import PyMongoError

logger = logging.getLogger("agentic_api")


class DocumentRegistry:
    """Async repository over the `documents` collection."""

    def __init__(self, client: AsyncIOMotorClient, db: AsyncIOMotorDatabase) -> None:
        self.client = client
        self._documents = db["documents"]

    @classmethod
    async def connect(cls, client: AsyncIOMotorClient, db: AsyncIOMotorDatabase) -> "DocumentRegistry":
        store = cls(client, db)
        await store._ensure_indexes()
        return store

    async def _ensure_indexes(self) -> None:
        await self._documents.create_index("user_id")

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
        try:
            await self._documents.insert_one(
                {
                    "_id": document_id,
                    "user_id": user_id,
                    "original_name": original_name,
                    "stored_name": stored_name,
                    "file_type": file_type,
                    "status": status,
                    "chunks_indexed": chunks_indexed,
                    "created_at": time.time(),
                }
            )
        except PyMongoError:
            logger.exception("Failed to register document %s for user %s", document_id, user_id)

    async def get(self, document_id: str) -> dict[str, Any] | None:
        try:
            return await self._documents.find_one({"_id": document_id})
        except PyMongoError:
            logger.exception("Failed to load document %s", document_id)
            return None

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        try:
            cursor = self._documents.find({"user_id": user_id}).sort("created_at", -1)
            return [
                {
                    "document_id": doc["_id"],
                    "original_name": doc.get("original_name"),
                    "file_type": doc.get("file_type"),
                    "status": doc.get("status"),
                    "chunks_indexed": doc.get("chunks_indexed"),
                    "created_at": doc.get("created_at"),
                }
                async for doc in cursor
            ]
        except PyMongoError:
            logger.exception("Failed to list documents for user %s", user_id)
            return []

    async def delete(self, document_id: str, user_id: str) -> bool:
        """Deletes only if `user_id` owns the document. Returns whether it was deleted."""
        try:
            result = await self._documents.delete_one({"_id": document_id, "user_id": user_id})
            return result.deleted_count > 0
        except PyMongoError:
            logger.exception("Failed to delete document %s for user %s", document_id, user_id)
            return False

    async def is_owner(self, document_id: str, user_id: str) -> bool:
        doc = await self.get(document_id)
        return doc is not None and doc.get("user_id") == user_id
