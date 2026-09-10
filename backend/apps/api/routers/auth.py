"""Sign-up / sign-in / sign-out and "who am I".

Real accounts (bcrypt-hashed passwords) backed by backend.apps.api.auth.UserStore.
When AUTH_DEV_BYPASS is on, signup/login are unreachable in practice (the
frontend skips the sign-in screen) and /me always returns the seeded dev
account regardless of any token.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.apps.api.auth import AuthUser, UserStore, get_current_user
from backend.apps.api.schemas import AuthResponse, CurrentUserResponse, LoginRequest, SignupRequest
from backend.config import settings as app_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _require_store(request: Request) -> UserStore:
    store = getattr(request.app.state, "user_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Auth store is unavailable")
    return store


@router.post("/signup", response_model=AuthResponse, status_code=201)
async def signup(body: SignupRequest, request: Request) -> AuthResponse:
    store = _require_store(request)
    user_id, token = await store.signup(body.email, body.password)
    return AuthResponse(user_id=user_id, email=body.email, token=token)


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, request: Request) -> AuthResponse:
    store = _require_store(request)
    user_id, token = await store.login(body.email, body.password)
    return AuthResponse(user_id=user_id, email=body.email, token=token)


@router.post("/logout", status_code=204)
async def logout(request: Request) -> None:
    if app_settings.auth.dev_bypass:
        return
    store = _require_store(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        await store.logout(header[len("bearer "):].strip())


@router.get("/me", response_model=CurrentUserResponse)
async def me(user: AuthUser = Depends(get_current_user)) -> CurrentUserResponse:
    return CurrentUserResponse(user_id=user.user_id, email=user.email)
