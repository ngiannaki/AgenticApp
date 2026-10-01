"""Phase 4: invite-only sign-up, token expiry/renewal, login rate limit,
daily quotas, background indexing and streamed replies."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from backend.apps.api import quotas
from backend.apps.api.auth import LoginLimiter, UserStore, hash_token
from backend.apps.api.document_registry import DocumentRegistry
from backend.apps.api.indexer import Indexer
from backend.apps.api.routers import auth as auth_router
from backend.apps.api.streaming_agent import _StreamedMessage
from backend.config import settings

run = asyncio.run


def _auth_client(pg_pool) -> TestClient:
    app = FastAPI()
    app.state.user_store = UserStore(pg_pool)
    app.include_router(auth_router.router)
    return TestClient(app)


def test_signup_needs_the_invite_code_and_can_be_disabled(pg_pool, monkeypatch) -> None:
    monkeypatch.setattr(settings.auth, "invite_code", "let-me-in")
    client = _auth_client(pg_pool)
    assert client.get("/api/auth/config").json() == {"signup_enabled": True, "invite_required": True}
    body = {"email": "inv@example.com", "password": "password123"}
    assert client.post("/api/auth/signup", json=body).status_code == 403
    assert client.post("/api/auth/signup", json={**body, "invite_code": "let-me-in"}).status_code == 201
    monkeypatch.setattr(settings.auth, "signup_enabled", False)
    assert client.post("/api/auth/signup", json={**body, "email": "x@example.com", "invite_code": "let-me-in"}).status_code == 403


def test_login_is_rate_limited(pg_pool, monkeypatch) -> None:
    monkeypatch.setattr(auth_router, "_limiter", LoginLimiter(3))
    client = _auth_client(pg_pool)
    run(UserStore(pg_pool).signup("rl@example.com", "password123"))
    bad = {"email": "rl@example.com", "password": "wrong-password"}
    assert [client.post("/api/auth/login", json=bad).status_code for _ in range(4)] == [401, 401, 401, 429]
    # Locked even with the right password until the window passes.
    assert client.post("/api/auth/login", json={**bad, "password": "password123"}).status_code == 429


def test_tokens_expire_and_renew_while_used(pg_pool) -> None:
    store = UserStore(pg_pool)
    _, token = run(store.signup("ttl@example.com", "password123"))
    with pg_pool.connection() as conn:
        row = conn.execute("SELECT expires_at > now() + interval '29 days' AS ok FROM app.auth_tokens WHERE token_hash = %s",
                           (hash_token(token),)).fetchone()
        assert row["ok"]
        # Stale use -> renewed on the next request.
        conn.execute("UPDATE app.auth_tokens SET expires_at = now() + interval '1 day', last_used_at = now() - interval '2 hours'"
                     " WHERE token_hash = %s", (hash_token(token),))
    assert run(store.resolve_token(token)) is not None
    with pg_pool.connection() as conn:
        assert conn.execute("SELECT expires_at > now() + interval '29 days' AS ok FROM app.auth_tokens WHERE token_hash = %s",
                            (hash_token(token),)).fetchone()["ok"]
        conn.execute("UPDATE app.auth_tokens SET expires_at = now() - interval '1 second' WHERE token_hash = %s", (hash_token(token),))
    assert run(store.resolve_token(token)) is None


def test_daily_quota(pg_pool, make_user, monkeypatch) -> None:
    monkeypatch.setattr(settings.api, "daily_uploads", 2)
    user = make_user()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pg_pool)))
    run(quotas.consume(request, user, "uploads"))
    run(quotas.consume(request, user, "uploads"))
    with pytest.raises(HTTPException) as exc:
        run(quotas.consume(request, user, "uploads"))
    assert exc.value.status_code == 429
    run(quotas.consume(request, user, "chat_turns"))  # unlimited (0)


def test_background_indexing_sets_status(pg_pool, make_user) -> None:
    user = make_user()
    registry = DocumentRegistry(pg_pool)

    class Runtime:
        async def call_tool_isolated(self, name, args):
            if args["filename"] == "bad.pdf":
                raise RuntimeError("parser exploded")
            return SimpleNamespace(content=[SimpleNamespace(text='{"success": true, "document_id": "%s", "chunks_indexed": 7}' % args["document_id"])])

    async def scenario():
        for doc_id, name in (("bg-ok", "good.pdf"), ("bg-bad", "bad.pdf")):
            await registry.create(document_id=doc_id, user_id=user, original_name=name, stored_name="original.pdf",
                                  file_type="pdf", status="indexing", chunks_indexed=0)
        indexer = Indexer(Runtime(), registry)
        assert await indexer.requeue_pending() >= 2
        while indexer._tasks:
            await asyncio.sleep(0.05)
        return await registry.get("bg-ok"), await registry.get("bg-bad")

    ok, bad = run(scenario())
    assert ok["status"] == "indexed" and ok["chunks_indexed"] == 7
    assert bad["status"] == "failed"


def test_streamed_message_is_rebuilt() -> None:
    def chunk(**delta):
        d = SimpleNamespace(**{"content": None, "tool_calls": None, "model_extra": {}, **delta})
        return SimpleNamespace(choices=[SimpleNamespace(delta=d)])

    def tc(index, id=None, name=None, args=None):
        return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=args))

    acc = _StreamedMessage()
    tokens = [acc.add(c) for c in (
        chunk(content="Hel"), chunk(content="lo"),
        chunk(tool_calls=[tc(0, "call_1", "retrieval_search", '{"que')]),
        chunk(tool_calls=[tc(0, None, None, 'ry": "x"}')]),
        SimpleNamespace(choices=[]),
    )]
    assert "".join(tokens) == "Hello"
    message = acc.message()
    assert message.content == "Hello"
    assert message.tool_calls[0].function.arguments == '{"query": "x"}' and message.tool_calls[0].id == "call_1"
