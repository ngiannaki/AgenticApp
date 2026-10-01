"""Run an MCP server over HTTP behind the internal service token.

Every MCP server (indexing, retrieval, db, code) trusts the `user_id` the API
puts in a tool call, so only the app's own services may call them. When
MCP_INTERNAL_TOKEN is set, every request must carry
`Authorization: Bearer <token>`; the API adds that header to each server
entry in servers.json automatically (backend/apps/api/main.py). Unset =
open (with a warning), so an old local setup keeps working.
"""
from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path

import uvicorn
from dotenv import dotenv_values
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


@lru_cache(maxsize=1)
def internal_token() -> str:
    """From the environment (compose, docker run -e) or backend/.env (the
    ps1 host processes, which don't export .env into os.environ)."""
    token = os.environ.get("MCP_INTERNAL_TOKEN")
    if token is None and _ENV_FILE.is_file():
        token = dotenv_values(_ENV_FILE).get("MCP_INTERNAL_TOKEN")
    return (token or "").strip()


class InternalTokenMiddleware:
    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            given = headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
            if not secrets.compare_digest(given, self.token):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def run_mcp_http(mcp, host: str, port: int) -> None:
    token = internal_token()
    middleware = [Middleware(InternalTokenMiddleware, token=token)] if token else []
    if not token:
        logger.warning("MCP_INTERNAL_TOKEN is not set: %s accepts unauthenticated calls on %s:%s", mcp.name, host, port)
    uvicorn.run(mcp.http_app(middleware=middleware), host=host, port=port)
