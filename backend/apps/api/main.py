"""FastAPI SSE backend for the generic MCP agent.

Wires together:
  - a single shared MCP connection (fastmcp.Client over every server in
    servers.json), opened once at startup and reused by every session,
  - Postgres (backend/core/db): migrations applied at startup; users,
    conversations and the document registry live there,
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

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.apps.api.auth import UserStore
from backend.apps.api.config import get_api_settings
from backend.apps.api.document_registry import DocumentRegistry
from backend.apps.api.logging_config import setup_logging
from backend.apps.api.persistence import ConversationStore
from backend.apps.api.routers import agents, auth, chat, conversations, data, files, internal, interrupts, questions, sessions, tools
from backend.apps.api.agent_tasks import AgentHub, TaskStore
from backend.apps.api.sessions import SessionManager
# ROLLBACK: swapped from `from backend.apps.api.streaming_agent import
# AgentRuntime` to try the v2 orchestrator/executor/critic runtime live. To
# revert, restore that import and change `OrchestratedAgentRuntime.connect`
# back to `AgentRuntime.connect` below — nothing else in this file, or any
# router, references the runtime class name directly.
from backend.apps.api.streaming_agent import AgentRuntime
from backend.config import settings as app_settings
from backend.core.mcp_http import internal_token
from backend.core.db import apply_migrations, close_pool, ensure_agent_role, get_pool, run_sync

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
        elif "url" in entry and internal_token():
            # HTTP MCP servers require the internal token (backend/core/mcp_http.py).
            entry["headers"] = {**entry.get("headers", {}), "Authorization": f"Bearer {internal_token()}"}
    return {"mcpServers": mapping}


async def _idle_session_reaper(app: FastAPI, interval_seconds: float) -> None:
    session_manager: SessionManager = app.state.session_manager
    while True:
        await asyncio.sleep(interval_seconds)
        stale = await session_manager.evict_idle()
        if stale:
            logger.info("Evicted %d idle session(s): %s", len(stale), ", ".join(stale))


def _ping_database(pool) -> None:
    with pool.connection() as conn:
        conn.execute("SELECT 1")


async def _connect_database():
    """Apply migrations, make sure the DB-tool role exists, open the pool.

    Postgres is the one hard dependency (users, conversations, documents and
    the vector index all live there), so startup waits up to ~30s for it (a
    container may still be booting) and then fails loudly instead of running
    half-working.
    """
    db = app_settings.database
    last_error: Exception | None = None
    for attempt in range(1, 16):
        try:
            applied = await run_sync(apply_migrations, db.url)
            if applied:
                logger.info("Applied database migrations: %s", ", ".join(applied))
            break
        except psycopg.OperationalError as exc:
            last_error = exc
            logger.warning("Postgres not reachable yet (attempt %d/15): %s", attempt, exc)
            await asyncio.sleep(2)
    else:
        raise RuntimeError(
            f"Could not connect to Postgres at DATABASE_URL after 30s — is it running? ({last_error})"
        )

    if db.agent_role_password:
        await run_sync(ensure_agent_role, db.url, db.agent_role, db.agent_role_password)
    else:
        logger.warning(
            "DATABASE_AGENT_ROLE_PASSWORD is not set — not managing the %r role the DB MCP server "
            "connects as; make sure it exists with access only to its own ws_* schemas.",
            db.agent_role,
        )
    return await run_sync(get_pool)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_api_settings()
    setup_logging(settings.log_level)

    # Database first: the MCP servers expect the schema to exist.
    pool = await _connect_database()
    app.state.db_pool = pool

    servers = _load_servers(settings.servers_path)
    logger.info("Connecting to MCP servers: %s", ", ".join(servers["mcpServers"]))
    runtime = await AgentRuntime.connect(servers, settings)

    app.state.runtime = runtime
    app.state.session_manager = SessionManager(settings.session_idle_timeout_seconds, runtime)

    app.state.conversation_store = ConversationStore(pool)
    app.state.user_store = UserStore(pool)
    app.state.document_registry = DocumentRegistry(pool)
    runtime.document_registry = app.state.document_registry

    if app_settings.auth.dev_bypass:
        app.state.dev_user = await app.state.user_store.seed_dev_user(
            app_settings.auth.dev_bypass_email, app_settings.auth.dev_bypass_password
        )
        logger.warning(
            "AUTH_DEV_BYPASS is on — every request is treated as %s. Do not use this in a "
            "deployment reachable by more than one person.",
            app.state.dev_user.email,
        )

    app.state.agent_hub = None
    agents_cfg = app_settings.agents
    if agents_cfg.enabled and agents_cfg.internal_token:
        task_store = TaskStore(pool)
        interrupted = await task_store.fail_interrupted()
        if interrupted:
            logger.info("Marked %d agent task(s) interrupted by the restart as failed", interrupted)
        app.state.agent_hub = AgentHub(task_store, agents_cfg.url, agents_cfg.internal_token, agents_cfg.max_active_tasks_per_user)
    else:
        logger.warning("Agents disabled: set AGENTS_INTERNAL_TOKEN (same value for the API and the agents service).")

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
        if app.state.agent_hub is not None:
            await app.state.agent_hub.close()
        await runtime.close()
        logger.info("MCP connection closed")
        close_pool()
        logger.info("Postgres pool closed")


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
    app.include_router(internal.router)
    app.include_router(agents.router)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        """Actually probes the MCP connection and Postgres, instead of just
        reporting the tool count captured once at startup -- that alone
        can't tell you the shared MCP client died (see the 2026-10-01
        ClosedResourceError incident, where /api/health kept reporting "ok"
        while every real tool call was 502ing). Returns 503 if either
        dependency is unreachable so uptime monitoring/orchestration can
        actually tell."""
        runtime = app.state.runtime
        checks: dict[str, str] = {}

        try:
            await asyncio.wait_for(runtime.mcp.ping(), timeout=5)
            checks["mcp"] = "ok"
        except Exception as exc:  # noqa: BLE001 — surfaced in the response body, not a 500
            checks["mcp"] = f"error: {exc}"

        try:
            await asyncio.wait_for(run_sync(_ping_database, app.state.db_pool), timeout=5)
            checks["postgres"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["postgres"] = f"error: {exc}"

        healthy = all(v == "ok" for v in checks.values())
        body = {"status": "ok" if healthy else "degraded", "tools": len(runtime.tools), "checks": checks}
        return JSONResponse(body, status_code=200 if healthy else 503)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_api_settings()
    uvicorn.run("backend.apps.api.main:app", host=settings.host, port=settings.port, reload=False)
