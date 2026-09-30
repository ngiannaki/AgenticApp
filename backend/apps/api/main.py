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
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.apps.api.auth import UserStore
from backend.apps.api.config import get_api_settings
from backend.apps.api.document_registry import DocumentRegistry
from backend.apps.api.logging_config import setup_logging
from backend.apps.api.persistence import ConversationStore
from backend.apps.api.routers import auth, chat, conversations, data, files, interrupts, questions, sessions, tools
from backend.apps.api.sessions import SessionManager
# ROLLBACK: swapped from `from backend.apps.api.streaming_agent import
# AgentRuntime` to try the v2 orchestrator/executor/critic runtime live. To
# revert, restore that import and change `OrchestratedAgentRuntime.connect`
# back to `AgentRuntime.connect` below — nothing else in this file, or any
# router, references the runtime class name directly.
from backend.apps.api.streaming_agent import AgentRuntime
from backend.config import settings as app_settings

logger = logging.getLogger("agentic_api")


def _load_servers(servers_path: Path) -> dict:
    raw = json.loads(servers_path.read_text(encoding="utf-8"))
    mapping = raw.get("mcpServers", raw)
    mapping = {k: v for k, v in mapping.items() if not k.startswith("_")}
    for entry in mapping.values():
        # For stdio-launched servers (servers.docker.json), the MCP client
        # only forwards a small safe-env allowlist to the child process by
        # default, not this process's actual environment — so an override
        # set at the container level (e.g. docker-compose's DB_MCP_DATABASE_URL
        # pointing at the "postgres" service instead of localhost) would
        # silently not reach the subprocess. These are our own trusted
        # subprocesses, not third-party servers, so full inheritance is safe
        # and matches how every server behaves when run standalone (each
        # just reads backend/.env directly).
        if "command" in entry:
            entry["env"] = {**os.environ, **entry.get("env", {})}
    return {"mcpServers": mapping}


async def _idle_session_reaper(app: FastAPI, interval_seconds: float) -> None:
    session_manager: SessionManager = app.state.session_manager
    while True:
        await asyncio.sleep(interval_seconds)
        stale = await session_manager.evict_idle()
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
    app.state.session_manager = SessionManager(settings.session_idle_timeout_seconds, runtime)

    try:
        app.state.conversation_store = await ConversationStore.connect(
            settings.mongo_url, settings.mongo_db_name
        )
        # Auth (users/tokens) and the document registry share the same
        # Mongo connection/database as conversation persistence.
        mongo_client = app.state.conversation_store.client
        mongo_db = mongo_client[settings.mongo_db_name]
        app.state.user_store = await UserStore.connect(mongo_client, mongo_db)
        app.state.document_registry = await DocumentRegistry.connect(mongo_client, mongo_db)
    except Exception:  # noqa: BLE001 — persistence is a durability nice-to-have, not a hard dependency
        logger.exception(
            "Could not connect to MongoDB at %s — conversations, auth, and documents will "
            "not be persisted until it is reachable. Start it via start_mcp_servers.ps1 or docker.",
            settings.mongo_url,
        )
        app.state.conversation_store = None
        app.state.user_store = None
        app.state.document_registry = None

    if app_settings.auth.dev_bypass:
        if app.state.user_store is not None:
            app.state.dev_user = await app.state.user_store.seed_dev_user(
                app_settings.auth.dev_bypass_email, app_settings.auth.dev_bypass_password
            )
        else:
            # Mongo unreachable but bypass is on: fall back to an in-memory
            # identity so local dev without Mongo still works end to end.
            from backend.apps.api.auth import AuthUser

            app.state.dev_user = AuthUser(user_id="dev-user", email=app_settings.auth.dev_bypass_email)
        logger.warning(
            "AUTH_DEV_BYPASS is on — every request is treated as %s. Do not use this in a "
            "deployment reachable by more than one person.",
            app.state.dev_user.email,
        )

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

    app.include_router(auth.router)
    app.include_router(sessions.router)
    app.include_router(chat.router)
    app.include_router(interrupts.router)
    app.include_router(questions.router)
    app.include_router(tools.router)
    app.include_router(conversations.router)
    app.include_router(files.router)
    app.include_router(files.documents_router)
    app.include_router(data.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "tools": len(app.state.runtime.tools)}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_api_settings()
    uvicorn.run("backend.apps.api.main:app", host=settings.host, port=settings.port, reload=False)
