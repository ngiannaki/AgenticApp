"""Email/password auth.

Accounts and bearer tokens are stored in the same MongoDB database already
used for conversation persistence (backend.apps.api.persistence) — two more
collections, `users` and `auth_tokens`. Tokens are opaque random strings, not
JWTs: a token is just a lookup key into `auth_tokens`, so revoking one (logout)
is a single delete, and there's nothing to verify a signature on.

AUTH_DEV_BYPASS=true (backend.config.settings.auth.dev_bypass) short-circuits
all of this: every request is treated as one seeded test account, no header
required. That account is created idempotently at startup (see seed_dev_user)
so it exists before the first bypassed request needs it.
"""
from __future__ import annotations

import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

import bcrypt
from fastapi import HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError, PyMongoError

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


class UserStore:
    """Async repository over the `users` and `auth_tokens` collections."""

    def __init__(self, client: AsyncIOMotorClient, db: AsyncIOMotorDatabase) -> None:
        self.client = client
        self._users = db["users"]
        self._tokens = db["auth_tokens"]

    @classmethod
    async def connect(cls, client: AsyncIOMotorClient, db: AsyncIOMotorDatabase) -> "UserStore":
        store = cls(client, db)
        await store._ensure_indexes()
        return store

    async def _ensure_indexes(self) -> None:
        await self._users.create_index("email", unique=True)

    async def signup(self, email: str, password: str) -> tuple[str, str]:
        """Create a new account. Returns (user_id, token). Raises HTTPException(409) on duplicate email."""
        user_id = str(uuid.uuid4())
        try:
            await self._users.insert_one(
                {
                    "_id": user_id,
                    "email": email,
                    "password_hash": hash_password(password),
                    "created_at": time.time(),
                }
            )
        except DuplicateKeyError:
            raise HTTPException(status_code=409, detail="An account with this email already exists")
        except PyMongoError:
            logger.exception("Failed to create account for %s", email)
            raise HTTPException(status_code=503, detail="Could not create account")
        token = await self._issue_token(user_id)
        logger.info("[AUTH] Signed up new user %s (%s)", user_id, email)
        return user_id, token

    async def login(self, email: str, password: str) -> tuple[str, str]:
        """Verify credentials. Returns (user_id, token). Raises HTTPException(401) on mismatch."""
        try:
            doc = await self._users.find_one({"email": email})
        except PyMongoError:
            logger.exception("Failed to look up account for %s", email)
            raise HTTPException(status_code=503, detail="Could not verify credentials")
        if doc is None or not verify_password(password, doc["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        token = await self._issue_token(doc["_id"])
        logger.info("[AUTH] User %s (%s) logged in", doc["_id"], email)
        return doc["_id"], token

    async def logout(self, token: str) -> None:
        try:
            await self._tokens.delete_one({"_id": token})
        except PyMongoError:
            logger.exception("Failed to delete auth token on logout")

    async def resolve_token(self, token: str) -> AuthUser | None:
        try:
            token_doc = await self._tokens.find_one({"_id": token})
        except PyMongoError:
            logger.exception("Failed to resolve auth token")
            return None
        if token_doc is None:
            return None
        try:
            user_doc = await self._users.find_one({"_id": token_doc["user_id"]})
        except PyMongoError:
            logger.exception("Failed to load user for token")
            return None
        if user_doc is None:
            return None
        return AuthUser(user_id=user_doc["_id"], email=user_doc["email"])

    async def seed_dev_user(self, email: str, password: str) -> AuthUser:
        """Idempotently ensure the dev-bypass account exists. Returns it either way."""
        try:
            existing = await self._users.find_one({"email": email})
        except PyMongoError:
            logger.exception("Failed to check for dev bypass user %s", email)
            existing = None
        if existing is not None:
            return AuthUser(user_id=existing["_id"], email=email)

        user_id = str(uuid.uuid4())
        try:
            await self._users.insert_one(
                {
                    "_id": user_id,
                    "email": email,
                    "password_hash": hash_password(password),
                    "created_at": time.time(),
                }
            )
            logger.info("[AUTH] Seeded dev bypass account %s (%s)", user_id, email)
        except DuplicateKeyError:
            existing = await self._users.find_one({"email": email})
            return AuthUser(user_id=existing["_id"], email=email)
        return AuthUser(user_id=user_id, email=email)

    async def _issue_token(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        await self._tokens.insert_one({"_id": token, "user_id": user_id, "created_at": time.time()})
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
