"""Endpoints for the app's own services, not for browsers.

GET /internal/files/{token}: the code sandbox redeems a single-use
`file_token` (issued by streaming_agent for one spreadsheet tool call, see
backend/apps/api/artifacts.py) for the bytes of the one document it grants.
No user auth — the token *is* the authorization: random, single-use,
expiring within minutes, and only ever issued for a document that just
passed the caller's ownership check. As defence in depth it's only served
to private/loopback addresses, and the edge proxies (Caddy/nginx) never
route /internal/ at all.
"""
from __future__ import annotations

import ipaddress
import logging
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.config import settings as app_settings
from backend.core.observability import LlmUsage

from backend.apps.api.routers.files import _document_store

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)
logger = logging.getLogger("agentic_api")


def _is_internal_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


@router.get("/files/{token}")
def redeem_file(token: str, request: Request) -> FileResponse:
    if not _is_internal_client(request):
        raise HTTPException(status_code=404)
    document_id = request.app.state.runtime.file_tokens.redeem(token)
    if document_id is None:
        raise HTTPException(status_code=404)
    path = _document_store.get_file_path(document_id)
    if path is None:
        raise HTTPException(status_code=404)
    logger.info("[INTERNAL] file token redeemed for document %s", document_id)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"X-Document-Id": document_id, "X-Document-Suffix": path.suffix.lower()},
    )


# --- tool access for the agents service ----------------------------------
# The agents (backend/apps/agents) never talk to MCP servers themselves:
# they call tools here, as the task's user, so ownership checks, approvals,
# sandbox file tokens and artifact handling are exactly the chat's.


def _require_agents_token(request: Request) -> None:
    expected = app_settings.agents.internal_token or ""
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(given, expected):
        raise HTTPException(status_code=401)


class ToolCall(BaseModel):
    user_id: str
    agent_id: str = ""
    task_id: str = ""
    name: str
    arguments: dict[str, Any] = {}
    approved: bool = False


@router.get("/tools")
def tool_schemas(request: Request, names: str = "") -> list[dict]:
    """Model-facing schemas (injected params already hidden) of the named tools."""
    _require_agents_token(request)
    wanted = {n for n in names.split(",") if n}
    return [t for t in request.app.state.runtime.tools if t["function"]["name"] in wanted]


@router.post("/tools/call")
async def call_tool(body: ToolCall, request: Request) -> dict:
    _require_agents_token(request)
    logger.info("[AGENT TOOL] agent=%s user=%s tool=%s", body.agent_id, body.user_id, body.name)
    ok, text = await request.app.state.runtime.execute_tool(
        body.user_id,
        body.name,
        body.arguments,
        approved=body.approved,
        trace_id=body.task_id,
        feature=f"agent:{body.agent_id}" if body.agent_id else "agent",
    )
    return {"ok": ok, "text": text}


class LlmUsageReport(BaseModel):
    """One model call made by the agents service, reported after the fact."""

    user_id: str
    agent_id: str = ""
    task_id: str = ""
    model: str
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    estimated: bool = False
    latency_ms: int | None = None
    status: str = "ok"
    error: str | None = None


@router.post("/usage")
async def report_usage(body: LlmUsageReport, request: Request) -> dict:
    """The agents service has no database of its own: it reports each model
    call here and the API records it (cost is worked out here, from the model)."""
    _require_agents_token(request)
    recorder = getattr(request.app.state, "usage", None)
    if recorder is not None:
        recorder.llm_call(
            user_id=body.user_id,
            feature=f"agent:{body.agent_id}" if body.agent_id else "agent",
            model=body.model,
            usage=LlmUsage(
                input_tokens=body.input_tokens,
                cached_tokens=body.cached_tokens,
                output_tokens=body.output_tokens,
                reasoning_tokens=body.reasoning_tokens,
                estimated=body.estimated,
            ),
            latency_ms=body.latency_ms,
            trace_id=body.task_id or None,
            task_id=body.task_id or None,
            status=body.status,
            error=body.error,
        )
    return {"ok": True}
