"""Sign-up / sign-in / sign-out and "who am I".

Real accounts (bcrypt-hashed passwords) backed by backend.apps.api.auth.UserStore.
When AUTH_DEV_BYPASS is on, signup/login are unreachable in practice (the
frontend skips the sign-in screen) and /me always returns the seeded dev
account regardless of any token.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.apps.api.auth import AuthUser, LoginLimiter, UserStore, get_current_user, normalize_email
from backend.apps.api.schemas import AuthResponse, CurrentUserResponse, LoginRequest, SignupRequest
from backend.config import settings as app_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
_limiter = LoginLimiter(app_settings.auth.login_max_failures)


def _client_ip(request: Request) -> str:
    # Behind Caddy / nginx / cloudflared the peer is the proxy.
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


def _require_store(request: Request) -> UserStore:
    store = getattr(request.app.state, "user_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Auth store is unavailable")
    return store


@router.post("/signup", response_model=AuthResponse, status_code=201)
async def signup(body: SignupRequest, request: Request) -> AuthResponse:
    store = _require_store(request)
    auth = app_settings.auth
    if not auth.signup_enabled:
        raise HTTPException(status_code=403, detail="Sign-up is disabled. Ask the admin for an account.")
    if auth.invite_code:
        ip = f"ip:{_client_ip(request)}"
        _limiter.check(ip)
        if not secrets.compare_digest(body.invite_code.strip(), auth.invite_code):
            _limiter.fail(ip)
            raise HTTPException(status_code=403, detail="Invalid invite code")
    user_id, token = await store.signup(body.email, body.password)
    return AuthResponse(user_id=user_id, email=normalize_email(body.email), token=token)


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, request: Request) -> AuthResponse:
    store = _require_store(request)
    keys = (f"ip:{_client_ip(request)}", f"email:{normalize_email(body.email)}")
    _limiter.check(*keys)
    try:
        user_id, token = await store.login(body.email, body.password)
    except HTTPException as exc:
        if exc.status_code == 401:
            _limiter.fail(*keys)
        raise
    _limiter.clear(keys[1])
    return AuthResponse(user_id=user_id, email=normalize_email(body.email), token=token)


@router.post("/logout", status_code=204)
async def logout(request: Request) -> None:
    if app_settings.auth.dev_bypass:
        return
    store = _require_store(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        await store.logout(header[len("bearer "):].strip())


@router.get("/config")
async def auth_config() -> dict:
    """What the sign-in screen should show (no auth needed)."""
    auth = app_settings.auth
    return {"signup_enabled": auth.signup_enabled, "invite_required": bool(auth.invite_code)}


@router.get("/me", response_model=CurrentUserResponse)
async def me(user: AuthUser = Depends(get_current_user)) -> CurrentUserResponse:
    return CurrentUserResponse(user_id=user.user_id, email=user.email)
