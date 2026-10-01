"""Process-wide psycopg connection pool."""
from __future__ import annotations

import threading
from typing import Any, Callable, TypeVar

import anyio
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from backend.config import settings

T = TypeVar("T")

_pool: ConnectionPool | None = None
_lock = threading.Lock()


def get_pool(conninfo: str | None = None) -> ConnectionPool:
    """The shared pool, opened on first use. Rows come back as dicts."""
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=conninfo or settings.database.url,
                    min_size=settings.database.pool_min_size,
                    max_size=settings.database.pool_max_size,
                    kwargs={"row_factory": dict_row},
                    open=True,
                )
    return _pool


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


async def run_sync(fn: Callable[..., T], *args: Any) -> T:
    """Run a blocking DB function on a worker thread (for async callers)."""
    return await anyio.to_thread.run_sync(fn, *args)
