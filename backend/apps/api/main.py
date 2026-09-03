"""FastAPI SSE backend for the generic MCP agent.

Wires together:
  - a single shared MCP connection (fastmcp.Client over every server in
    servers.json), opened once at startup and reused by every session,
  - a per-session conversation (backend/apps/api/sessions.py),
  - a streaming, interruptible tool-calling loop
    (backend/apps/api/streaming_agent.py) exposed to the frontend as SSE.

Run:
    python -m backend.apps.api.main
    # or, for reload during development:
    uvicorn backend.apps.api.main:app --reload --port 8100
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.apps.api.config import get_api_settings
from backend.apps.api.logging_config import setup_logging
from backend.apps.api.persistence import ConversationStore
from backend.apps.api.routers import chat, conversations, files, interrupts, sessions, tools
from backend.apps.api.sessions import SessionManager
from backend.apps.api.streaming_agent import AgentRuntime

logger = logging.getLogger("agentic_api")


def _load_servers(servers_path: Path) -> dict:
    raw = json.loads(servers_path.read_text(encoding="utf-8"))
    mapping = raw.get("mcpServers", raw)
    mapping = {k: v for k, v in mapping.items() if not k.startswith("_")}
    return {"mcpServers": mapping}


async def _idle_session_reaper(app: FastAPI, interval_seconds: float) -> None:
    session_manager: SessionManager = app.state.session_manager
    while True:
        await asyncio.sleep(interval_seconds)
        stale = session_manager.evict_idle()
        if stale:
            logger.info("Evicted %d idle session(s): %s", len(stale), ", ".join(stale))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_api_settings()
    setup_logging(settings.log_level)

    servers = _load_servers(settings.servers_path)
    logger.info("Connecting to MCP servers: %s", ", ".join(servers["mcpServers"]))
    runtime = await AgentRuntime.connect(servers, settings)

    app.state.runtime = runtime
    app.state.session_manager = SessionManager(settings.session_idle_timeout_seconds)

    try:
        app.state.conversation_store = await ConversationStore.connect(
            settings.mongo_url, settings.mongo_db_name
        )
    except Exception:  # noqa: BLE001 — persistence is a durability nice-to-have, not a hard dependency
        logger.exception(
            "Could not connect to MongoDB at %s — conversations will not be persisted "
            "until it is reachable. Start it via start_mcp_servers.ps1 or docker.",
            settings.mongo_url,
        )
        app.state.conversation_store = None

    reaper_task = asyncio.create_task(
        _idle_session_reaper(app, interval_seconds=min(300.0, settings.session_idle_timeout_seconds))
    )

    logger.info("Agentic API ready — %d tool(s) available", len(runtime.tools))
    try:
        yield
    finally:
        reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper_task
        await runtime.close()
        logger.info("MCP connection closed")
        if app.state.conversation_store is not None:
            await app.state.conversation_store.close()
            logger.info("MongoDB connection closed")


def create_app() -> FastAPI:
    settings = get_api_settings()
    app = FastAPI(title="Agentic App API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(sessions.router)
    app.include_router(chat.router)
    app.include_router(interrupts.router)
    app.include_router(tools.router)
    app.include_router(conversations.router)
    app.include_router(files.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "tools": len(app.state.runtime.tools)}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_api_settings()
    uvicorn.run("backend.apps.api.main:app", host=settings.host, port=settings.port, reload=False)
