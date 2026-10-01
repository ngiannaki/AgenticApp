"""One-time copy of the old MongoDB data into Postgres.

    pip install pymongo   # no longer an app dependency
    python -m backend.scripts.migrate_mongo_to_pg --mongo-url mongodb://localhost:27017 --mongo-db agentic_chat
    python -m backend.scripts.reindex_all

Copies users (password hashes included, so everyone keeps their password),
conversations with all their messages, and the document registry. Login
tokens are not copied — everyone signs in once more. Safe to re-run: rows
that already exist are left alone.

Emails are lowercased on the way in. If two old accounts differ only by
case, the first one wins and the second one's conversations/documents are
attached to it.

Vectors aren't copied (Chroma's format doesn't carry over) — run
reindex_all afterwards to rebuild them from the original files in
data/documents.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from backend.config import settings
from backend.core.db import apply_migrations, get_pool


def _ts(value) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(tz=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mongo-url", default="mongodb://localhost:27017")
    parser.add_argument("--mongo-db", default="agentic_chat")
    args = parser.parse_args(argv)

    try:
        from pymongo import MongoClient
    except ImportError:
        print("pymongo is required for this one-time migration: pip install pymongo", file=sys.stderr)
        return 1

    apply_migrations(settings.database.url)
    mongo = MongoClient(args.mongo_url, serverSelectionTimeoutMS=5000)[args.mongo_db]
    pool = get_pool()
    counts = {"users": 0, "conversations": 0, "messages": 0, "documents": 0}

    with pool.connection() as conn, conn.transaction():
        # users: old id -> id actually kept in Postgres (case-duplicate emails merge)
        user_map: dict[str, str] = {}
        for doc in mongo["users"].find().sort("created_at", 1):
            email = str(doc["email"]).strip().lower()
            existing = conn.execute("SELECT id FROM app.users WHERE email = %s", (email,)).fetchone()
            if existing:
                user_map[doc["_id"]] = existing["id"]
                continue
            conn.execute(
                "INSERT INTO app.users (id, email, password_hash, created_at) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (id) DO NOTHING",
                (doc["_id"], email, doc["password_hash"], _ts(doc.get("created_at"))),
            )
            user_map[doc["_id"]] = doc["_id"]
            counts["users"] += 1

        for doc in mongo["conversations"].find():
            owner = user_map.get(doc.get("user_id"))
            if owner is None:
                continue  # conversation of a user that doesn't exist (e.g. old dev-bypass id)
            cur = conn.execute(
                "INSERT INTO app.conversations (id, user_id, title, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (doc["_id"], owner, doc.get("title") or "New chat", _ts(doc.get("created_at")), _ts(doc.get("updated_at"))),
            )
            if cur.rowcount == 0:
                continue  # already migrated — don't duplicate its messages
            counts["conversations"] += 1
            for msg in mongo["messages"].find({"conversation_id": doc["_id"]}).sort("created_at", 1):
                conn.execute(
                    "INSERT INTO app.messages (id, conversation_id, role, content, steps, created_at)"
                    " VALUES (%s, %s, %s, %s, %s::jsonb, %s) ON CONFLICT (id) DO NOTHING",
                    (
                        msg["_id"],
                        doc["_id"],
                        msg.get("role", "assistant"),
                        msg.get("content") or "",
                        json.dumps(msg.get("steps") or [], default=str),
                        _ts(msg.get("created_at")),
                    ),
                )
                counts["messages"] += 1

        for doc in mongo["documents"].find():
            owner = user_map.get(doc.get("user_id"))
            if owner is None:
                continue
            cur = conn.execute(
                "INSERT INTO app.documents"
                " (id, user_id, original_name, stored_name, file_type, status, chunks_indexed, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (
                    doc["_id"],
                    owner,
                    doc.get("original_name"),
                    doc.get("stored_name"),
                    doc.get("file_type"),
                    doc.get("status") or "indexed",
                    doc.get("chunks_indexed") or 0,
                    _ts(doc.get("created_at")),
                ),
            )
            counts["documents"] += cur.rowcount

    print("Copied: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    print("Next: python -m backend.scripts.reindex_all   (rebuilds the vector index from data/documents)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
