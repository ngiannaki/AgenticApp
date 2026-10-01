"""Per-user daily quotas (app.usage_daily), so one person can't spend the
whole LLM budget. Limits come from API_DAILY_CHAT_TURNS, API_DAILY_UPLOADS
and API_DAILY_AGENT_TASKS; 0 = unlimited. Over the limit -> 429."""
from __future__ import annotations

from fastapi import HTTPException, Request

from backend.config import settings as app_settings
from backend.core.db import run_sync

_LABELS = {"chat_turns": "chat messages", "uploads": "uploads", "agent_tasks": "agent tasks"}


def _consume(pool, user_id: str, kind: str, limit: int) -> bool:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO app.usage_daily (user_id, kind, count) VALUES (%s, %s, 1)"
            " ON CONFLICT (user_id, day, kind) DO UPDATE SET count = app.usage_daily.count + 1"
            " WHERE app.usage_daily.count < %s RETURNING count",
            (user_id, kind, limit),
        ).fetchone()
    return row is not None


async def consume(request: Request, user_id: str, kind: str) -> None:
    """Count one `kind` use for today; 429 when the daily limit is reached."""
    limit = getattr(app_settings.api, f"daily_{kind}", 0)
    pool = getattr(request.app.state, "db_pool", None)
    if not limit or pool is None:
        return
    if not await run_sync(_consume, pool, user_id, kind, limit):
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit reached: {limit} {_LABELS.get(kind, kind)} per day. It resets at midnight.",
        )
