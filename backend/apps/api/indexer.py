"""Background indexing for Documents-page uploads: one document at a time
(Docling + embeddings are heavy), status kept in app.documents
(indexing -> indexed | failed). Documents still "indexing" when the API
restarts are re-queued at startup."""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

logger = logging.getLogger("agentic_api")


class Indexer:
    def __init__(self, runtime, registry, concurrency: int = 1) -> None:
        self.runtime = runtime
        self.registry = registry
        self._slots = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task] = set()

    def submit(self, document_id: str, filename: str, user_id: str) -> None:
        task = asyncio.create_task(self._run(document_id, filename, user_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def requeue_pending(self) -> int:
        pending = await self.registry.with_status("indexing")
        for doc in pending:
            self.submit(doc["id"], doc["original_name"], doc["user_id"])
        return len(pending)

    async def _run(self, document_id: str, filename: str, user_id: str) -> None:
        from backend.apps.api.routers.files import index_stored_document

        async with self._slots:
            try:
                data = await index_stored_document(self.runtime, document_id, filename, user_id)
            except HTTPException as exc:
                logger.warning("Indexing %s (%s) failed: %s", filename, document_id, exc.detail)
                await self.registry.set_status(document_id, "failed", error=str(exc.detail)[:500])
                return
            except Exception as exc:  # noqa: BLE001 — never leave a document stuck in "indexing"
                logger.exception("Indexing %s (%s) crashed", filename, document_id)
                await self.registry.set_status(document_id, "failed", error=str(exc)[:500])
                return
            await self.registry.set_status(document_id, "indexed", chunks_indexed=data.get("chunks_indexed", 0))
            logger.info("Indexed %s (%s): %s chunk(s)", filename, document_id, data.get("chunks_indexed", 0))

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
