"""Usage / latency / cost tracking: pricing, recording, the chat loop's hooks,
the retrieval timings, and (with Postgres) the usage queries end to end."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.apps.api.routers import usage as usage_router
from backend.apps.api.sessions import ChatSession
from backend.apps.api.streaming_agent import AgentRuntime, _StreamedMessage
from backend.config import settings
from backend.config.pricing import compute_cost, price_for
from backend.core.observability import LlmUsage, UsageRecorder, estimate_usage, usage_from_response

run = asyncio.run


# --- pricing -----------------------------------------------------------------


def test_cost_bills_cached_tokens_at_the_cached_rate() -> None:
    # gpt-4o-mini: 0.15 in, 0.075 cached, 0.60 out per 1M
    assert compute_cost("gpt-4o-mini", 1_000_000, 500_000, 100_000) == pytest.approx(0.5 * 0.15 + 0.5 * 0.075 + 0.1 * 0.6)


def test_longest_model_prefix_wins_and_unknown_models_have_no_price() -> None:
    assert price_for("gpt-4o-mini-2024-07-18") == price_for("gpt-4o-mini")
    assert price_for("gpt-4o-2024-08-06") == price_for("gpt-4o")
    assert compute_cost("llama3.2:3b", 1000, 0, 1000) is None


def test_pricing_can_be_overridden_and_a_bad_override_is_ignored() -> None:
    override = json.dumps({"my-model": {"input": 1.0, "output": 2.0}, "gpt-4o": {"input": 9, "output": 9}})
    assert compute_cost("my-model-v2", 1_000_000, 0, 1_000_000, override) == pytest.approx(3.0)
    assert price_for("gpt-4o", override) == (9.0, 9.0, 9.0)
    assert price_for("gpt-4o-mini", "not json") == price_for("gpt-4o-mini")


# --- token usage ----------------------------------------------------------------


def test_usage_is_read_from_an_openai_usage_object() -> None:
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=40),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
    )
    assert usage_from_response(usage) == LlmUsage(100, 40, 20, 5, False)
    assert usage_from_response({"prompt_tokens": 3, "completion_tokens": 4}) == LlmUsage(3, 0, 4, 0, False)
    assert usage_from_response(None) is None
    assert usage_from_response(SimpleNamespace(prompt_tokens=0, completion_tokens=0)) is None  # server reported nothing


def test_missing_usage_is_estimated_and_flagged() -> None:
    estimated = estimate_usage([{"role": "user", "content": "x" * 400}], "y" * 40)
    assert estimated.estimated and estimated.input_tokens > 100 and estimated.output_tokens == 10


def test_streamed_usage_arrives_on_a_chunk_without_choices() -> None:
    acc = _StreamedMessage()
    delta = SimpleNamespace(content="hi", tool_calls=None, reasoning=None, model_extra=None)
    acc.add(SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None))
    acc.add(SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2)))
    assert acc.usage.prompt_tokens == 7 and acc.message().content == "hi"


# --- the recorder -------------------------------------------------------------------


class FakePool:
    """Records what would be written; `fail` makes every write raise."""

    def __init__(self, fail: bool = False) -> None:
        self.writes: list[tuple] = []
        self.fail = fail

    def connection(self):
        pool = self

        class Conn:
            def __enter__(self):
                if pool.fail:
                    raise RuntimeError("database is down")
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                pool.writes.append((sql, params))
                return SimpleNamespace(rowcount=2)

        return Conn()


def test_recorder_writes_a_row_and_returns_the_cost(monkeypatch) -> None:
    monkeypatch.setattr(settings.obs, "enabled", True)
    pool = FakePool()
    cost = UsageRecorder(pool).llm_call(
        user_id="u1", feature="chat", model="gpt-4o-mini", usage=LlmUsage(1000, 0, 100), latency_ms=250, trace_id="t1"
    )
    (sql, params), = pool.writes
    assert "INSERT INTO app.llm_calls" in sql
    assert params[0] == "u1" and params[1] == "t1" and params[4] == "chat" and params[10] == 250
    if settings.is_prod:
        assert cost == pytest.approx((1000 * 0.15 + 100 * 0.6) / 1_000_000)
    else:
        assert cost == 0.0  # a local model is free


def test_recorder_does_nothing_when_disabled_or_anonymous(monkeypatch) -> None:
    pool = FakePool()
    recorder = UsageRecorder(pool)
    monkeypatch.setattr(settings.obs, "enabled", False)
    recorder.llm_call(user_id="u1", feature="chat", model="m")
    recorder.span(user_id="u1", feature="chat", name="tool", duration_ms=1)
    monkeypatch.setattr(settings.obs, "enabled", True)
    recorder.llm_call(user_id="", feature="chat", model="m")
    assert pool.writes == []


def test_a_failing_database_never_breaks_the_caller(monkeypatch) -> None:
    monkeypatch.setattr(settings.obs, "enabled", True)
    recorder = UsageRecorder(FakePool(fail=True))
    recorder.llm_call(user_id="u1", feature="chat", model="m", usage=LlmUsage(1, 0, 1))  # must not raise
    recorder.span(user_id="u1", feature="chat", name="t", duration_ms=1)


def test_recorder_from_async_code_writes_on_a_worker_thread(monkeypatch) -> None:
    monkeypatch.setattr(settings.obs, "enabled", True)
    pool = FakePool()

    async def go() -> None:
        UsageRecorder(pool).span(user_id="u1", feature="chat", name="t", duration_ms=3, attrs={"timings_ms": {"rerank": 9}})
        await asyncio.sleep(0.2)

    run(go())
    (sql, params), = pool.writes
    assert "trace_spans" in sql and json.loads(params[-1]) == {"timings_ms": {"rerank": 9}}


# --- the chat loop's hooks -------------------------------------------------------------


def _runtime(recorder) -> AgentRuntime:
    runtime = object.__new__(AgentRuntime)
    runtime.usage = recorder
    return runtime


def _session() -> ChatSession:
    session = object.__new__(ChatSession)
    session.user_id, session.session_id, session.trace_id, session.turn_usage = "u1", "conv-1", "trace-1", {}
    return session


class SpyRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.spans: list[dict] = []

    def llm_call(self, **kw):
        self.calls.append(kw)
        return 0.01

    def span(self, **kw):
        self.spans.append(kw)


def test_a_model_call_is_recorded_and_added_to_the_turn_totals() -> None:
    spy, session = SpyRecorder(), _session()
    runtime = _runtime(spy)
    message = SimpleNamespace(content="answer", tool_calls=None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(prompt_tokens=50, completion_tokens=10)
    )
    runtime._record_chat_call(session, [{"role": "user", "content": "q"}], response, started=0.0, first_token_at=None)
    runtime._record_chat_call(session, [], None, started=0.0, first_token_at=None, error="boom")

    ok, failed = spy.calls
    assert ok["usage"].input_tokens == 50 and ok["trace_id"] == "trace-1" and ok["conversation_id"] == "conv-1"
    assert failed["status"] == "error" and failed["error"] == "boom"
    assert session.turn_usage["llm_calls"] == 2 and session.turn_usage["input_tokens"] == 50
    assert session.turn_usage["cost_usd"] == pytest.approx(0.02)


def test_a_server_that_reports_no_usage_gets_an_estimate() -> None:
    spy, session = SpyRecorder(), _session()
    message = SimpleNamespace(content="z" * 80, tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
    _runtime(spy)._record_chat_call(session, [{"role": "user", "content": "q" * 200}], response, 0.0, None)
    assert spy.calls[0]["usage"].estimated and spy.calls[0]["usage"].output_tokens == 20
    assert session.turn_usage["estimated"] is True


def test_an_unpriced_model_is_flagged_in_the_turn_summary() -> None:
    class Unpriced(SpyRecorder):
        def llm_call(self, **kw):
            return None

    session = _session()
    message = SimpleNamespace(content="a", tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(prompt_tokens=5, completion_tokens=1))
    runtime = _runtime(Unpriced())
    runtime._record_chat_call(session, [], response, 0.0, None)
    event = runtime._usage_event(session, turn_started=0.0)
    assert event["event"] == "usage" and json.loads(event["data"])["cost_usd"] is None


def test_tool_calls_are_timed_with_retrieval_stage_timings() -> None:
    spy = SpyRecorder()
    runtime = _runtime(spy)
    payload = json.dumps({"success": True, "timings_ms": {"embed": 12, "rerank": 80}})

    class Mcp:
        async def call_tool(self, name, arguments):
            return payload

    result = run(runtime._timed_tool_call(Mcp(), "retrieval_search", {}, 5, user_id="u1", trace_id="t", feature="chat"))
    assert result == payload
    (span,) = spy.spans
    assert span["name"] == "retrieval_search" and span["status"] == "ok" and span["trace_id"] == "t"
    assert span["attrs"] == {"timings_ms": {"embed": 12, "rerank": 80}}


def test_failed_and_slow_tool_calls_are_recorded_and_still_raise() -> None:
    spy = SpyRecorder()
    runtime = _runtime(spy)

    class Broken:
        async def call_tool(self, name, arguments):
            raise ValueError("bad sql")

    class Slow:
        async def call_tool(self, name, arguments):
            await asyncio.sleep(1)

    with pytest.raises(ValueError):
        run(runtime._timed_tool_call(Broken(), "db_query", {}, 5, user_id="u1", trace_id="t", feature="chat"))
    with pytest.raises(asyncio.TimeoutError):
        run(runtime._timed_tool_call(Slow(), "code_run", {}, 0.05, user_id="u1", trace_id="t", feature="chat"))
    assert [s["status"] for s in spy.spans] == ["error", "timeout"]
    assert spy.spans[0]["attrs"]["error"] == "bad sql"


# --- the usage API ------------------------------------------------------------------------


def test_admin_is_by_email_list(monkeypatch) -> None:
    monkeypatch.setattr(settings.auth, "dev_bypass", False)
    monkeypatch.setattr(settings.obs, "admin_emails", "Boss@Example.com, other@example.com")
    assert usage_router.is_admin(SimpleNamespace(email="boss@example.com"))
    assert not usage_router.is_admin(SimpleNamespace(email="intern@example.com"))
    monkeypatch.setattr(settings.auth, "dev_bypass", True)
    assert usage_router.is_admin(SimpleNamespace(email="intern@example.com"))  # single-user local dev


def test_usage_query_end_to_end(pg_pool, make_user, monkeypatch) -> None:
    monkeypatch.setattr(settings.obs, "enabled", True)
    alice, bob = make_user(), make_user()
    recorder = UsageRecorder(pg_pool)
    for user, tokens in ((alice, 100), (alice, 300), (bob, 50)):
        recorder._write(
            "INSERT INTO app.llm_calls (user_id, feature, model, input_tokens, output_tokens, latency_ms, cost_usd)"
            " VALUES (%s, 'chat', 'gpt-4o-mini', %s, 10, 200, 0.5)",
            (user, tokens),
        )
    recorder._write(
        "INSERT INTO app.llm_calls (user_id, feature, model, input_tokens, output_tokens, status)"
        " VALUES (%s, 'agent:research', 'mystery-model', 5, 5, 'ok')",  # no price -> cost NULL
        (alice,),
    )
    recorder._write(
        "INSERT INTO app.trace_spans (user_id, feature, kind, name, duration_ms, status, attrs)"
        " VALUES (%s, 'chat', 'tool', 'retrieval_retrieve', 120, 'ok', %s::jsonb),"
        "        (%s, 'chat', 'tool', 'retrieval_retrieve', 80, 'error', '{}'::jsonb)",
        (alice, json.dumps({"timings_ms": {"embed": 10, "rerank": 50}}), alice),
    )

    mine = usage_router._usage(pg_pool, alice, 7)
    assert mine["totals"]["calls"] == 3 and mine["totals"]["input_tokens"] == 405
    assert mine["totals"]["cost_usd"] == pytest.approx(1.0) and mine["totals"]["unpriced_calls"] == 1
    assert len(mine["by_day"]) == 7 and mine["by_day"][-1]["calls"] == 3
    assert {f["name"] for f in mine["by_feature"]} == {"chat", "agent:research"}
    tool = mine["tools"][0]
    assert tool["name"] == "retrieval_retrieve" and tool["calls"] == 2 and tool["errors"] == 1
    assert [s["stage"] for s in mine["retrieval_stages"]] == ["embed", "rerank"]
    assert mine["by_user"] == []

    everyone = usage_router._usage(pg_pool, None, 7)
    assert everyone["totals"]["calls"] >= 4 and len(everyone["by_user"]) >= 2

    assert UsageRecorder(pg_pool).purge(retention_days=1) >= 0


def test_purge_removes_only_old_rows(pg_pool, make_user) -> None:
    user = make_user()
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO app.llm_calls (user_id, ts, feature, model) VALUES (%s, now() - interval '100 days', 'chat', 'm'),"
            " (%s, now(), 'chat', 'm')",
            (user, user),
        )
    UsageRecorder(pg_pool).purge(retention_days=30)
    with pg_pool.connection() as conn:
        remaining = conn.execute("SELECT count(*) AS n FROM app.llm_calls WHERE user_id = %s", (user,)).fetchone()["n"]
    assert remaining == 1
