"""The one-time Mongo -> Postgres copy script, against an in-memory fake
Mongo (mongomock). Needs TEST_DATABASE_ADMIN_URL (see conftest.py)."""
from __future__ import annotations

import pytest

mongomock = pytest.importorskip("mongomock")


@pytest.fixture
def fake_mongo(monkeypatch):
    client = mongomock.MongoClient()
    db = client["agentic_chat"]
    db["users"].insert_many([
        {"_id": "u-old-1", "email": "Carol@Example.com", "password_hash": "$2b$hash", "created_at": 1_700_000_000.0},
        {"_id": "u-old-2", "email": "carol@example.com", "password_hash": "$2b$other", "created_at": 1_700_000_100.0},
    ])
    db["conversations"].insert_many([
        {"_id": "c-1", "user_id": "u-old-1", "title": "Budget", "created_at": 1_700_000_200.0, "updated_at": 1_700_000_300.0},
        {"_id": "c-2", "user_id": "u-old-2", "title": "Merged", "created_at": 1_700_000_200.0, "updated_at": 1_700_000_300.0},
        {"_id": "c-orphan", "user_id": "dev-user", "title": "x", "created_at": 1.0, "updated_at": 1.0},
    ])
    db["messages"].insert_many([
        {"_id": "m-1", "conversation_id": "c-1", "role": "user", "content": "hi", "steps": [], "created_at": 1_700_000_201.0},
        {"_id": "m-2", "conversation_id": "c-1", "role": "assistant", "content": "hello", "steps": [{"type": "status"}], "created_at": 1_700_000_202.0},
    ])
    db["documents"].insert_one(
        {"_id": "d-1", "user_id": "u-old-2", "original_name": "r.pdf", "stored_name": "original.pdf",
         "file_type": "pdf", "status": "indexed", "chunks_indexed": 4, "created_at": 1_700_000_400.0}
    )
    import pymongo

    monkeypatch.setattr(pymongo, "MongoClient", lambda *a, **k: client)
    return client


def test_copies_users_conversations_messages_documents(pg_url, pg_pool, fake_mongo, monkeypatch) -> None:
    from backend.core.db import pool as pool_module
    from backend.scripts import migrate_mongo_to_pg

    monkeypatch.setattr(pool_module, "_pool", pg_pool)
    monkeypatch.setattr(migrate_mongo_to_pg.settings.database, "url", pg_url)

    assert migrate_mongo_to_pg.main([]) == 0
    assert migrate_mongo_to_pg.main([]) == 0  # re-run is a no-op

    with pg_pool.connection() as conn:
        users = conn.execute("SELECT id, email FROM app.users WHERE email = 'carol@example.com'").fetchall()
        assert [u["id"] for u in users] == ["u-old-1"]
        convs = {r["id"]: r["user_id"] for r in conn.execute("SELECT id, user_id FROM app.conversations WHERE id LIKE 'c-%'")}
        assert convs == {"c-1": "u-old-1", "c-2": "u-old-1"}  # case-duplicate account merged; orphan skipped
        msgs = conn.execute("SELECT id, steps FROM app.messages WHERE conversation_id = 'c-1' ORDER BY seq").fetchall()
        assert [m["id"] for m in msgs] == ["m-1", "m-2"] and msgs[1]["steps"] == [{"type": "status"}]
        doc = conn.execute("SELECT user_id, status FROM app.documents WHERE id = 'd-1'").fetchone()
        assert doc == {"user_id": "u-old-1", "status": "indexed"}
