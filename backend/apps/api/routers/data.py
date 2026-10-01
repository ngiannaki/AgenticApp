"""Read-only REST access to the db MCP server's tables — backs the "Data"
page, where a signed-in user can browse tables created via the agent's
create_table tool (e.g. while working a document) without going through
chat. Calls the MCP tool directly, never through the LLM — same idea as
routers/files.py's indexing calls, but deliberately NOT via
AgentRuntime.call_tool_isolated: even now that call_tool_isolated no longer
re-triggers a stdio reconnect per call (it shares the process-wide client
under stdio transport too — see AgentRuntime.acquire_session_client), it
still does real per-call work for the isolated-sessions/HTTP case (opening
and closing a fresh connection). This router's calls are one-shot, stateless
request/response (list/describe/query), with none of the pending-interrupt
or streaming state that made per-session clients necessary for chat (see
sessions.py's ChatSession), and get hit on every list/select/paginate click
— so it skips that entirely and reuses `request.app.state.runtime.mcp` — the
same single client opened once at app startup — directly, under both
transports. Sharing this client across concurrent requests is safe.

Tables are per user: every call passes the signed-in user's id, and the db
MCP server confines it to that user's own workspace schema (see
backend/mcp_servers/db/database/workspace.py) — nobody sees anyone else's
tables here.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.apps.api.auth import AuthUser, get_current_user
from backend.mcp_servers.db.security.identifier_validator import quote_identifier

logger = logging.getLogger("agentic_api")

router = APIRouter(prefix="/api/data", tags=["data"])

_MAX_LIMIT = 500


def _tool_result_json(result) -> dict:
    """Mirror streaming_agent._extract_text's unwrapping, then parse the JSON
    text every db MCP tool returns."""
    content = getattr(result, "content", result)
    if isinstance(content, list):
        text = "\n".join(b.text for b in content if hasattr(b, "text"))
    else:
        text = str(content)
    return json.loads(text)


async def _call_db_tool(request: Request, tool_name: str, arguments: dict) -> dict:
    runtime = request.app.state.runtime
    try:
        result = await runtime.mcp.call_tool(tool_name, arguments)
    except Exception as exc:  # noqa: BLE001 — surface as a normal HTTP error, not a 500 stack trace
        logger.exception("%s call failed", tool_name)
        raise HTTPException(status_code=502, detail=f"{tool_name} failed: {exc}")

    data = _tool_result_json(result)
    if not data.get("success"):
        raise HTTPException(status_code=400, detail=data.get("error", f"{tool_name} failed"))
    return data


@router.get("/tables")
async def list_tables(request: Request, user: AuthUser = Depends(get_current_user)) -> list[dict]:
    """Every table in the signed-in user's own workspace that they can read."""
    data = await _call_db_tool(request, "db_list_tables", {"user_id": user.user_id})
    return [t for t in data.get("tables", []) if t.get("select")]


@router.get("/tables/{table}/schema")
async def get_table_schema(
    table: str, request: Request, user: AuthUser = Depends(get_current_user)
) -> dict:
    return await _call_db_tool(request, "db_describe_table", {"table": table, "user_id": user.user_id})


@router.get("/tables/{table}/rows")
async def get_table_rows(
    table: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: int = Query(100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict:
    # `table` is an untrusted path param — describe_table both confirms the
    # table exists and enforces `select` permission (permissions.check inside
    # the MCP tool raises if not), so its success is exactly the check we'd
    # otherwise need a separate db_list_tables round trip for. Never
    # interpolate `table` into SQL on the strength of the request alone —
    # quote_identifier below re-validates independently regardless.
    described = await _call_db_tool(request, "db_describe_table", {"table": table, "user_id": user.user_id})
    table = described["table"]  # validated/lowercased identifier

    quoted = quote_identifier(table, kind="table")
    data = await _call_db_tool(
        request,
        "db_query_data",
        {"sql": f"SELECT * FROM {quoted} LIMIT %s OFFSET %s", "parameters": [limit, offset], "user_id": user.user_id},
    )
    return {
        "columns": data.get("columns", []),
        "rows": data.get("rows", []),
        "row_count": data.get("row_count", 0),
        "truncated": data.get("truncated", False),
    }
