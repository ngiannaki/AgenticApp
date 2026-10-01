"""Per-user workspaces: every user's DB-tool tables live in their own
Postgres schema, `ws_<hash of user_id>`.

The server never trusts the model to pick a schema. `server._handle` derives
the schema from the `user_id` the API injects into every call and sets it
for the duration of that one tool call (a ContextVar, so concurrent calls
can't see each other's); everything below it — introspection queries, the
SQL validator's existence checks, and `transaction()`'s
`SET LOCAL search_path` — reads it from here. The SQL validator rejects
schema-qualified names, so a query can only ever resolve against the
caller's own schema (plus Postgres' built-in pg_catalog, which the validator
separately guards).
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from backend.mcp_servers.db.errors import ValidationError

_current_schema: ContextVar[str | None] = ContextVar("db_mcp_workspace_schema", default=None)


def workspace_schema_for(user_id: str) -> str:
    """Stable, identifier-safe schema name for a user (lowercase hex, 23 chars)."""
    return "ws_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:20]


def current_schema() -> str:
    schema = _current_schema.get()
    if schema is None:
        # Every tool goes through server._handle, which always sets this; reaching
        # here without it is a bug, and the safe failure is to refuse.
        raise ValidationError("No workspace is active for this call (missing user_id).")
    return schema


@contextmanager
def use_workspace(user_id: str) -> Iterator[str]:
    if not user_id:
        raise ValidationError("user_id is required (it's supplied by the application, not the model).")
    token = _current_schema.set(workspace_schema_for(user_id))
    try:
        yield _current_schema.get()  # type: ignore[misc]
    finally:
        _current_schema.reset(token)
