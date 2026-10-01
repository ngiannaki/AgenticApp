"""The API's Postgres-backed stores: users/tokens, conversations/messages,
and the document registry. Needs TEST_DATABASE_ADMIN_URL (see conftest.py)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from backend.apps.api.auth import UserStore, hash_token
from backend.apps.api.document_registry import DocumentRegistry
from backend.apps.api.persistence import DEFAULT_TITLE, ConversationStore
from backend.core.db import apply_migrations


def run(coro):
    return asyncio.run(coro)


def test_migrations_are_idempotent(pg_url) -> None:
    assert apply_migrations(pg_url) == []


def test_signup_login_and_tokens_are_hashed(pg_pool) -> None:
    store = UserStore(pg_pool)
    user_id, token = run(store.signup("  Alice@Example.COM ", "correct horse"))

    with pg_pool.connection() as conn:
        row = conn.execute("SELECT email FROM app.users WHERE id = %s", (user_id,)).fetchone()
        stored = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM app.auth_tokens WHERE user_id = %s", (user_id,))]
    assert row["email"] == "alice@example.com"
    assert token not in stored and hash_token(token) in stored

    assert run(store.resolve_token(token)).user_id == user_id
    login_id, token2 = run(store.login("ALICE@example.com", "correct horse"))
    assert login_id == user_id and token2 != token

    with pytest.raises(HTTPException) as exc:
        run(store.signup("alice@example.com", "another password"))
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        run(store.login("alice@example.com", "wrong password"))
    assert exc.value.status_code == 401

    run(store.logout(token))
    assert run(store.resolve_token(token)) is None
    assert run(store.resolve_token(token2)) is not None


def test_expired_token_is_rejected(pg_pool) -> None:
    store = UserStore(pg_pool)
    _, token = run(store.signup("expiring@example.com", "password123"))
    with pg_pool.connection() as conn:
        conn.execute("UPDATE app.auth_tokens SET expires_at = now() - interval '1 minute' WHERE token_hash = %s", (hash_token(token),))
    assert run(store.resolve_token(token)) is None


def test_seed_dev_user_is_idempotent(pg_pool) -> None:
    store = UserStore(pg_pool)
    first = run(store.seed_dev_user("Dev@Local.test", "pw"))
    second = run(store.seed_dev_user("dev@local.test", "pw"))
    assert first.user_id == second.user_id


def test_conversations_and_messages(pg_pool, make_user) -> None:
    store = ConversationStore(pg_pool)
    owner = make_user()
    run(store.create_conversation("conv-1", owner))
    conv = run(store.get_conversation("conv-1"))
    assert conv["user_id"] == owner and conv["title"] == DEFAULT_TITLE

    run(store.set_title_if_default("conv-1", "What is in my report?" * 10))
    run(store.set_title_if_default("conv-1", "second title is ignored"))
    assert run(store.get_conversation("conv-1"))["title"] == ("What is in my report?" * 10)[:80]

    run(store.append_message("conv-1", "user", "hi"))
    run(store.append_message("conv-1", "assistant", "hello", [{"type": "status", "message": "x"}]))
    messages = run(store.get_messages("conv-1"))
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["steps"] == [{"type": "status", "message": "x"}]

    listed = run(store.list_conversations(owner))
    assert [c["id"] for c in listed] == ["conv-1"] and isinstance(listed[0]["updated_at"], float)
    assert run(store.list_conversations(make_user())) == []

    run(store.delete_conversation("conv-1"))
    assert run(store.get_conversation("conv-1")) is None
    assert run(store.get_messages("conv-1")) == []


def test_document_registry_ownership(pg_pool, make_user) -> None:
    registry = DocumentRegistry(pg_pool)
    alice, bob = make_user(), make_user()
    run(registry.create("doc-1", alice, "report.pdf", "original.pdf", "pdf", "indexed", 12))

    assert run(registry.is_owner("doc-1", alice))
    assert not run(registry.is_owner("doc-1", bob))
    assert not run(registry.is_owner("missing", alice))
    assert [d["document_id"] for d in run(registry.list_for_user(alice))] == ["doc-1"]
    assert run(registry.list_for_user(bob)) == []

    run(registry.set_status("doc-1", "failed", error="boom"))
    assert run(registry.get("doc-1"))["status"] == "failed"

    assert not run(registry.delete("doc-1", bob))
    assert run(registry.delete("doc-1", alice))
    assert run(registry.get("doc-1")) is None
