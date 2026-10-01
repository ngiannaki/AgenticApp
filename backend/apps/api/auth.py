"""Email/password auth.

Accounts and bearer tokens live in Postgres (app.users / app.auth_tokens,
see backend/migrations/0001_init.sql). Tokens are opaque random strings, not
JWTs: a token is a lookup key, so revoking one (logout) is a single delete.
Only a SHA-256 of each token is stored — the raw token exists only in the
client — so a database leak doesn't hand out live sessions. Emails are
stored lowercased so "Alice@x.com" and "alice@x.com" are one account.

AUTH_DEV_BYPASS=true (backend.config.settings.auth.dev_bypass) short-circuits
all of this: every request is treated as one seeded test account, no header
required. That account is created idempotently at startup (see seed_dev_user)
so it exists before the first bypassed request needs it.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass

import bcrypt
import psycopg
from fastapi import HTTPException, Request
from psycopg_pool import ConnectionPool

from backend.core.db import run_sync

logger = logging.getLogger("agentic_api")


@dataclass
class AuthUser:
    user_id: str
    email: str


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed stored hash — treat as "doesn't match" rather than raising.
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


class UserStore:
    """Repository over app.users and app.auth_tokens."""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    async def signup(self, email: str, password: str) -> tuple[str, str]:
        """Create a new account. Returns (user_id, token). Raises HTTPException(409) on duplicate email."""
        email = normalize_email(email)
        user_id = str(uuid.uuid4())
        password_hash = await run_sync(hash_password, password)

        def _insert() -> None:
            with self.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO app.users (id, email, password_hash) VALUES (%s, %s, %s)",
                    (user_id, email, password_hash),
                )

        try:
            await run_sync(_insert)
        except psycopg.errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="An account with this email already exists")
        except psycopg.Error:
            logger.exception("Failed to create account for %s", email)
            raise HTTPException(status_code=503, detail="Could not create account")
        token = await self._issue_token(user_id)
        logger.info("[AUTH] Signed up new user %s (%s)", user_id, email)
        return user_id, token

    async def login(self, email: str, password: str) -> tuple[str, str]:
        """Verify credentials. Returns (user_id, token). Raises HTTPException(401) on mismatch."""
        email = normalize_email(email)

        def _find():
            with self.pool.connection() as conn:
                return conn.execute(
                    "SELECT id, password_hash FROM app.users WHERE email = %s", (email,)
                ).fetchone()

        try:
            row = await run_sync(_find)
        except psycopg.Error:
            logger.exception("Failed to look up account for %s", email)
            raise HTTPException(status_code=503, detail="Could not verify credentials")
        if row is None or not await run_sync(verify_password, password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        token = await self._issue_token(row["id"])
        logger.info("[AUTH] User %s (%s) logged in", row["id"], email)
        return row["id"], token

    async def logout(self, token: str) -> None:
        def _delete() -> None:
            with self.pool.connection() as conn:
                conn.execute("DELETE FROM app.auth_tokens WHERE token_hash = %s", (hash_token(token),))

        try:
            await run_sync(_delete)
        except psycopg.Error:
            logger.exception("Failed to delete auth token on logout")

    async def resolve_token(self, token: str) -> AuthUser | None:
        def _resolve():
            with self.pool.connection() as conn:
                return conn.execute(
                    "SELECT u.id, u.email FROM app.auth_tokens t JOIN app.users u ON u.id = t.user_id"
                    " WHERE t.token_hash = %s AND (t.expires_at IS NULL OR t.expires_at > now())",
                    (hash_token(token),),
                ).fetchone()

        try:
            row = await run_sync(_resolve)
        except psycopg.Error:
            logger.exception("Failed to resolve auth token")
            return None
        return AuthUser(user_id=row["id"], email=row["email"]) if row else None

    async def seed_dev_user(self, email: str, password: str) -> AuthUser:
        """Idempotently ensure the dev-bypass account exists. Returns it either way."""
        email = normalize_email(email)
        password_hash = await run_sync(hash_password, password)

        def _upsert():
            with self.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO app.users (id, email, password_hash) VALUES (%s, %s, %s)"
                    " ON CONFLICT (email) DO NOTHING",
                    (str(uuid.uuid4()), email, password_hash),
                )
                return conn.execute("SELECT id FROM app.users WHERE email = %s", (email,)).fetchone()

        row = await run_sync(_upsert)
        logger.info("[AUTH] Dev bypass account is %s (%s)", row["id"], email)
        return AuthUser(user_id=row["id"], email=email)

    async def _issue_token(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)

        def _insert() -> None:
            with self.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO app.auth_tokens (token_hash, user_id) VALUES (%s, %s)",
                    (hash_token(token), user_id),
                )

        await run_sync(_insert)
        return token


async def get_current_user(request: Request) -> AuthUser:
    """FastAPI dependency: resolve the authenticated caller.

    Bypassed entirely (no header needed) when settings.auth.dev_bypass is on —
    every request becomes the seeded dev account, set on app.state at startup.
    """
    from backend.config import settings as app_settings

    if app_settings.auth.dev_bypass:
        dev_user: AuthUser = request.app.state.dev_user
        return dev_user

    user_store: UserStore | None = getattr(request.app.state, "user_store", None)
    if user_store is None:
        raise HTTPException(status_code=503, detail="Auth store is unavailable")

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = header[len("bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    user = await user_store.resolve_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user
