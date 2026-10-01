"""Shared fixtures.

Tests marked with the `pg_url` / `pg_pool` fixtures need a real Postgres with
the pgvector extension available, reached through TEST_DATABASE_ADMIN_URL —
a superuser connection string, e.g.
    postgresql://postgres:postgres@localhost:5432/postgres
(CI starts a pgvector/pgvector:pg16 service for this). A fresh
`agentic_test` database is created for the test session. Without the
variable those tests are skipped, so the rest of the suite still runs
anywhere.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import pytest

TEST_DB_NAME = "agentic_test"


def _with_db(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


@pytest.fixture(scope="session")
def pg_url() -> str:
    admin_url = os.environ.get("TEST_DATABASE_ADMIN_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_ADMIN_URL not set — skipping Postgres integration tests")

    import psycopg
    from psycopg import sql

    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB_NAME)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB_NAME)))
    url = _with_db(admin_url, TEST_DB_NAME)

    from backend.core.db import apply_migrations

    apply_migrations(url)
    return url


@pytest.fixture(scope="session")
def pg_pool(pg_url: str):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(conninfo=pg_url, min_size=1, max_size=4, kwargs={"row_factory": dict_row}, open=True)
    yield pool
    pool.close()


@pytest.fixture
def make_user(pg_pool):
    """Insert a bare user row (FK target for conversations/documents)."""
    import uuid

    def make(email: str | None = None) -> str:
        user_id = str(uuid.uuid4())
        with pg_pool.connection() as conn:
            conn.execute(
                "INSERT INTO app.users (id, email, password_hash) VALUES (%s, %s, 'x')",
                (user_id, email or f"{user_id}@test.local"),
            )
        return user_id

    return make
