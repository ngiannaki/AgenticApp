"""Usage, cost and latency, from app.llm_calls / app.trace_spans
(see backend/core/observability.py).

GET /api/usage/me     the signed-in user's own numbers
GET /api/usage/admin  everyone's, plus a per-user breakdown — only for emails in
                      OBS_ADMIN_EMAILS (or any user when AUTH_DEV_BYPASS is on,
                      which is single-user local dev)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.apps.api.auth import AuthUser, get_current_user
from backend.config import settings as app_settings
from backend.core.db import run_sync

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _window(alias: str = "") -> str:
    return f"{alias}ts >= now() - make_interval(days => %(days)s)"


_STAGE_ORDER = ["embed", "dense", "keyword", "fuse", "rerank", "expand"]


def is_admin(user: AuthUser) -> bool:
    return app_settings.auth.dev_bypass or user.email.lower() in app_settings.obs.admin_email_set


def _usage(pool: Any, user_id: str | None, days: int) -> dict[str, Any]:
    """All the dashboard numbers for the last `days` days; `user_id` None = everyone."""
    def scope(alias: str = "") -> str:
        return f"AND {alias}user_id = %(user_id)s" if user_id else ""

    params = {"days": days, "user_id": user_id}
    with pool.connection() as conn:
        totals = conn.execute(
            f"""SELECT count(*)::int AS calls,
                       coalesce(sum(input_tokens), 0)::bigint AS input_tokens,
                       coalesce(sum(cached_tokens), 0)::bigint AS cached_tokens,
                       coalesce(sum(output_tokens), 0)::bigint AS output_tokens,
                       coalesce(sum(cost_usd), 0)::float8 AS cost_usd,
                       (count(*) FILTER (WHERE cost_usd IS NULL AND status = 'ok'))::int AS unpriced_calls,
                       (count(*) FILTER (WHERE status <> 'ok'))::int AS errors,
                       (count(*) FILTER (WHERE estimated))::int AS estimated_calls,
                       coalesce(avg(latency_ms), 0)::float8 AS avg_latency_ms,
                       coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms), 0)::float8 AS p95_latency_ms
                FROM app.llm_calls WHERE {_window()} {scope()}""",  # noqa: S608 - fixed fragments
            params,
        ).fetchone()

        by_day = conn.execute(
            f"""SELECT (ts AT TIME ZONE 'UTC')::date AS day,
                       count(*)::int AS calls,
                       coalesce(sum(input_tokens + output_tokens), 0)::bigint AS tokens,
                       coalesce(sum(cost_usd), 0)::float8 AS cost_usd
                FROM app.llm_calls WHERE {_window()} {scope()} GROUP BY 1 ORDER BY 1""",  # noqa: S608
            params,
        ).fetchall()

        def grouped(column: str) -> list[dict[str, Any]]:
            return conn.execute(
                f"""SELECT {column} AS name,
                           count(*)::int AS calls,
                           coalesce(sum(input_tokens), 0)::bigint AS input_tokens,
                           coalesce(sum(output_tokens), 0)::bigint AS output_tokens,
                           coalesce(sum(cost_usd), 0)::float8 AS cost_usd,
                           coalesce(avg(latency_ms), 0)::float8 AS avg_latency_ms
                    FROM app.llm_calls WHERE {_window()} {scope()}
                    GROUP BY 1 ORDER BY 5 DESC, 3 DESC LIMIT 20""",  # noqa: S608
                params,
            ).fetchall()

        by_feature, by_model = grouped("feature"), grouped("model")

        tools = conn.execute(
            f"""SELECT name,
                       count(*)::int AS calls,
                       (count(*) FILTER (WHERE status <> 'ok'))::int AS errors,
                       avg(duration_ms)::float8 AS avg_ms,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::float8 AS p95_ms
                FROM app.trace_spans WHERE kind = 'tool' AND {_window()} {scope()}
                GROUP BY name ORDER BY calls DESC LIMIT 20""",  # noqa: S608
            params,
        ).fetchall()

        stages = conn.execute(
            f"""SELECT t.key AS stage, avg(t.value::float8)::float8 AS avg_ms
                FROM app.trace_spans s,
                     jsonb_each_text(CASE WHEN jsonb_typeof(s.attrs -> 'timings_ms') = 'object'
                                          THEN s.attrs -> 'timings_ms' ELSE '{{}}'::jsonb END) AS t
                WHERE s.kind = 'tool' AND {_window('s.')} {scope('s.')}
                GROUP BY t.key""",  # noqa: S608
            params,
        ).fetchall()

        conversations = conn.execute(
            f"""SELECT l.conversation_id AS id, coalesce(c.title, '(deleted chat)') AS title,
                       count(*)::int AS calls,
                       coalesce(sum(l.input_tokens + l.output_tokens), 0)::bigint AS tokens,
                       coalesce(sum(l.cost_usd), 0)::float8 AS cost_usd
                FROM app.llm_calls l LEFT JOIN app.conversations c ON c.id = l.conversation_id
                WHERE l.conversation_id IS NOT NULL AND {_window('l.')} {scope('l.')}
                GROUP BY l.conversation_id, c.title
                ORDER BY 5 DESC, 4 DESC LIMIT 8""",  # noqa: S608
            params,
        ).fetchall()

        by_user = []
        if user_id is None:
            by_user = conn.execute(
                f"""SELECT u.email AS name, count(*)::int AS calls,
                           coalesce(sum(l.input_tokens + l.output_tokens), 0)::bigint AS tokens,
                           coalesce(sum(l.cost_usd), 0)::float8 AS cost_usd
                    FROM app.llm_calls l JOIN app.users u ON u.id = l.user_id
                    WHERE {_window('l.')}
                    GROUP BY u.email ORDER BY 4 DESC, 3 DESC LIMIT 20""",  # noqa: S608
                params,
            ).fetchall()

    # One entry per day in the window (zeros where nothing happened), so a chart has no gaps.
    today = datetime.now(timezone.utc).date()
    seen = {row["day"]: row for row in by_day}
    series = []
    for offset in range(days - 1, -1, -1):
        day: date = today - timedelta(days=offset)
        row = seen.get(day)
        series.append(
            {
                "day": day.isoformat(),
                "calls": row["calls"] if row else 0,
                "tokens": row["tokens"] if row else 0,
                "cost_usd": row["cost_usd"] if row else 0.0,
            }
        )

    return {
        "days": days,
        "totals": totals,
        "by_day": series,
        "by_feature": by_feature,
        "by_model": by_model,
        "tools": tools,
        "retrieval_stages": sorted(stages, key=lambda r: _STAGE_ORDER.index(r["stage"]) if r["stage"] in _STAGE_ORDER else 99),
        "top_conversations": conversations,
        "by_user": by_user,
        "pricing_note": (
            "Local models are counted as $0. Models without a known price are listed under 'unpriced calls'."
            if totals["unpriced_calls"] or not app_settings.is_prod
            else None
        ),
    }


@router.get("/me")
async def my_usage(
    request: Request, days: int = Query(30, ge=1, le=90), user: AuthUser = Depends(get_current_user)
) -> dict[str, Any]:
    result = await run_sync(_usage, request.app.state.db_pool, user.user_id, days)
    return {**result, "scope": "me", "is_admin": is_admin(user)}


@router.get("/admin")
async def all_usage(
    request: Request, days: int = Query(30, ge=1, le=90), user: AuthUser = Depends(get_current_user)
) -> dict[str, Any]:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Only admins can see everyone's usage.")
    result = await run_sync(_usage, request.app.state.db_pool, None, days)
    return {**result, "scope": "all", "is_admin": True}
