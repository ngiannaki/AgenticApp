"""The DB MCP server's per-user workspaces: each user's tables live in their
own schema, other users' tables and the app's own schemas are unreachable,
and the server's role can't read app data at all. Needs
TEST_DATABASE_ADMIN_URL (see conftest.py)."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from backend.config.settings import DbMcpSettings
from backend.core.db import ensure_agent_role
from backend.mcp_servers.db.database.connection import Database
from backend.mcp_servers.db.database.workspace import use_workspace, workspace_schema_for
from backend.mcp_servers.db.errors import AuthorizationError, ValidationError
from backend.mcp_servers.db.models.mutations import InsertDataRequest
from backend.mcp_servers.db.models.queries import QueryDataRequest
from backend.mcp_servers.db.models.schema import CreateTableRequest
from backend.mcp_servers.db.security.permissions import PermissionsConfig
from backend.mcp_servers.db.tools.introspection import list_tables
from backend.mcp_servers.db.tools.mutations import insert_data
from backend.mcp_servers.db.tools.query import query_data
from backend.mcp_servers.db.tools.schema import create_table

AGENT_ROLE = "agent_ws_test"
AGENT_PASSWORD = "agent-test-pw"
PERMISSIONS_FILE = "backend/mcp_servers/db/permissions.yaml"


@pytest.fixture(scope="module")
def agent_db(pg_url):
    ensure_agent_role(pg_url, AGENT_ROLE, AGENT_PASSWORD)
    parts = urlsplit(pg_url)
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    agent_url = urlunsplit(parts._replace(netloc=f"{AGENT_ROLE}:{AGENT_PASSWORD}@{host}"))
    settings = DbMcpSettings(database_url=agent_url, pool_min_size=1, pool_max_size=2)
    db = Database(settings)
    yield db, settings, PermissionsConfig.load(PERMISSIONS_FILE), agent_url
    db.close()


def _in_workspace(database, user_id, fn, *args, **kwargs):
    with use_workspace(user_id) as schema:
        database.ensure_schema(schema)
        return fn(*args, **kwargs)


def _create_notes_table(db, settings, perms, user_id):
    request = CreateTableRequest(
        table="notes",
        columns=[{"name": "id", "type": "INTEGER", "primary_key": True}, {"name": "body", "type": "STRING"}],
        if_exists="ignore",
    )
    _in_workspace(db, user_id, create_table, request, settings=settings, permissions=perms, db=db)


def test_workspace_schema_name_is_stable_and_safe() -> None:
    name = workspace_schema_for("6f1c-user")
    assert name == workspace_schema_for("6f1c-user")
    assert name.startswith("ws_") and name.replace("_", "").isalnum() and name == name.lower()
    assert name != workspace_schema_for("other-user")


def test_missing_user_id_is_refused() -> None:
    with pytest.raises(ValidationError):
        with use_workspace(""):
            pass


def test_users_only_see_their_own_tables(agent_db) -> None:
    db, settings, perms, _ = agent_db
    _create_notes_table(db, settings, perms, "alice")
    _in_workspace(
        db, "alice", insert_data,
        InsertDataRequest(table="notes", records=[{"id": 1, "body": "alice secret"}], returning=False),
        settings=settings, permissions=perms, db=db,
    )

    alice_tables = _in_workspace(db, "alice", list_tables, permissions=perms, db=db)["tables"]
    bob_tables = _in_workspace(db, "bob", list_tables, permissions=perms, db=db)["tables"]
    assert [t["table"] for t in alice_tables] == ["notes"]
    assert bob_tables == []

    rows = _in_workspace(
        db, "alice", query_data, QueryDataRequest(sql="SELECT body FROM notes"),
        settings=settings, permissions=perms, db=db,
    )["rows"]
    assert rows == [{"body": "alice secret"}]

    with pytest.raises(ValidationError):  # bob has no `notes` table of his own
        _in_workspace(
            db, "bob", query_data, QueryDataRequest(sql="SELECT body FROM notes"),
            settings=settings, permissions=perms, db=db,
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM {alice}.notes",
        "SELECT * FROM app.users",
        "SELECT * FROM rag.chunks",
        "SELECT * FROM public.schema_migrations",
    ],
)
def test_schema_qualified_names_are_rejected(agent_db, sql) -> None:
    db, settings, perms, _ = agent_db
    _create_notes_table(db, settings, perms, "alice")
    with pytest.raises(AuthorizationError):
        _in_workspace(
            db, "bob", query_data, QueryDataRequest(sql=sql.format(alice=workspace_schema_for("alice"))),
            settings=settings, permissions=perms, db=db,
        )


def test_agent_role_cannot_read_app_data(agent_db) -> None:
    *_, agent_url = agent_db
    with psycopg.connect(agent_url) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM app.users")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM rag.chunks")


@pytest.mark.parametrize("sql", ["SELECT * FROM pg_tables", "SELECT query FROM pg_stat_activity", "SELECT * FROM pg_namespace"])
def test_unqualified_system_catalogs_are_refused(agent_db, sql) -> None:
    # Postgres always searches pg_catalog implicitly, so these would resolve
    # even with search_path pinned — and would list other users' schemas,
    # tables or running queries. The validator only accepts tables that
    # exist in the caller's own workspace.
    db, settings, perms, _ = agent_db
    with pytest.raises((ValidationError, AuthorizationError)):
        _in_workspace(
            db, "bob", query_data, QueryDataRequest(sql=sql),
            settings=settings, permissions=perms, db=db,
        )
