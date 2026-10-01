"""MCP servers over HTTP require the internal token (backend/core/mcp_http.py)."""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.core.mcp_http import InternalTokenMiddleware


def test_internal_token_required() -> None:
    app = Starlette(
        routes=[Route("/mcp", lambda r: PlainTextResponse("ok"), methods=["GET", "POST"])],
        middleware=[Middleware(InternalTokenMiddleware, token="s3cret")],
    )
    client = TestClient(app)
    assert client.post("/mcp").status_code == 401
    assert client.post("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/mcp", headers={"Authorization": "Bearer s3cret"}).text == "ok"


def test_api_sends_the_token_to_http_servers(tmp_path, monkeypatch) -> None:
    import json

    from backend.apps.api import main
    from backend.core import mcp_http

    mcp_http.internal_token.cache_clear()
    monkeypatch.setenv("MCP_INTERNAL_TOKEN", "s3cret")
    try:
        path = tmp_path / "servers.json"
        path.write_text(json.dumps({"mcpServers": {"db": {"url": "http://db:8002/mcp"}}}))
        servers = main._load_servers(path)["mcpServers"]
        assert servers["db"]["headers"]["Authorization"] == "Bearer s3cret"
    finally:
        mcp_http.internal_token.cache_clear()
