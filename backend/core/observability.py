"""Usage, latency and cost tracking: one row per LLM call (app.llm_calls) and
per tool call (app.trace_spans), see backend/migrations/0006_observability.sql.

Metadata only — no prompts, answers or tool arguments are stored. Writes run
on a worker thread and never raise: a failing database must not break a chat
turn. Local models (ENVIRONMENT=DEV, Ollama) are recorded with cost 0; in
PROD a model without a known price is recorded with cost NULL (see
backend/config/pricing.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from backend.config import settings
from backend.config.pricing import compute_cost

logger = logging.getLogger(__name__)

_MAX_ERROR_CHARS = 300


@dataclass
class LlmUsage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    estimated: bool = False


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def usage_from_response(usage: Any) -> LlmUsage | None:
    """Token counts from an OpenAI-style `usage` object (or dict); None when
    the server reported none (or all zeros)."""
    if usage is None:
        return None
    prompt = int(_get(usage, "prompt_tokens", 0) or 0)
    completion = int(_get(usage, "completion_tokens", 0) or 0)
    if not prompt and not completion:
        return None
    return LlmUsage(
        input_tokens=prompt,
        cached_tokens=int(_get(_get(usage, "prompt_tokens_details"), "cached_tokens", 0) or 0),
        output_tokens=completion,
        reasoning_tokens=int(_get(_get(usage, "completion_tokens_details"), "reasoning_tokens", 0) or 0),
    )


def estimate_tokens(text: str) -> int:
    """~4 characters per token. Only a fallback for servers that report no usage."""
    return max(1, len(text) // 4) if text else 0


def estimate_usage(messages: Any, output_text: str) -> LlmUsage:
    try:
        prompt_text = json.dumps(messages, default=str)
    except (TypeError, ValueError):
        prompt_text = str(messages)
    return LlmUsage(input_tokens=estimate_tokens(prompt_text), output_tokens=estimate_tokens(output_text), estimated=True)


def cost_of(model: str, usage: LlmUsage) -> float | None:
    """USD cost, 0 for a local model, None when a PROD model has no known price."""
    if not settings.is_prod:
        return 0.0
    return compute_cost(model, usage.input_tokens, usage.cached_tokens, usage.output_tokens, settings.obs.pricing_json)


class UsageRecorder:
    """Writes usage rows. Safe to call from async code (fire-and-forget on a
    worker thread) and from plain threads."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # --- writes ---------------------------------------------------------------

    def llm_call(
        self,
        *,
        user_id: str,
        feature: str,
        model: str,
        usage: LlmUsage | None = None,
        latency_ms: int | None = None,
        ttft_ms: int | None = None,
        trace_id: str | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        status: str = "ok",
        error: str | None = None,
    ) -> float | None:
        """Record one LLM call. Returns its cost (None = unknown price)."""
        usage = usage or LlmUsage()
        cost = cost_of(model, usage) if status == "ok" else 0.0
        if not settings.obs.enabled or not user_id:
            return cost
        row = (
            user_id, trace_id, conversation_id, task_id, feature, model,
            usage.input_tokens, usage.cached_tokens, usage.output_tokens, usage.reasoning_tokens,
            latency_ms, ttft_ms, cost, usage.estimated, status, (error or "")[:_MAX_ERROR_CHARS] or None,
        )
        self._submit(
            "INSERT INTO app.llm_calls (user_id, trace_id, conversation_id, task_id, feature, model,"
            " input_tokens, cached_tokens, output_tokens, reasoning_tokens, latency_ms, ttft_ms,"
            " cost_usd, estimated, status, error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            row,
        )
        return cost

    def span(
        self,
        *,
        user_id: str,
        feature: str,
        name: str,
        duration_ms: int,
        trace_id: str | None = None,
        kind: str = "tool",
        status: str = "ok",
        attrs: dict[str, Any] | None = None,
    ) -> None:
        if not settings.obs.enabled or not user_id:
            return
        self._submit(
            "INSERT INTO app.trace_spans (user_id, trace_id, feature, kind, name, duration_ms, status, attrs)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (user_id, trace_id, feature, kind, name, duration_ms, status, json.dumps(attrs or {}, default=str)),
        )

    def purge(self, retention_days: int | None = None) -> int:
        """Delete rows older than the retention period; returns rows deleted."""
        days = settings.obs.retention_days if retention_days is None else retention_days
        if not days:
            return 0
        deleted = 0
        with self._pool.connection() as conn:
            for table in ("app.llm_calls", "app.trace_spans"):
                deleted += conn.execute(
                    f"DELETE FROM {table} WHERE ts < now() - make_interval(days => %s)", (days,)  # noqa: S608 - fixed names
                ).rowcount
        return deleted

    # --- plumbing ---------------------------------------------------------------

    def _write(self, sql: str, params: tuple) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(sql, params)
        except Exception:  # noqa: BLE001 - tracking must never break a request
            logger.warning("Could not record usage", exc_info=True)

    def _submit(self, sql: str, params: tuple) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write(sql, params)
            return
        loop.run_in_executor(None, self._write, sql, params)
