"""Read-only introspection of the tools available across all connected MCP servers."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from backend.apps.api.auth import AuthUser, get_current_user

from backend.apps.api.schemas import ToolInfo

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("", response_model=list[ToolInfo])
def list_tools(request: Request, user: AuthUser = Depends(get_current_user)) -> list[ToolInfo]:
    runtime = request.app.state.runtime
    return [
        ToolInfo(
            name=t["function"]["name"],
            description=t["function"]["description"],
            parameters=t["function"]["parameters"] or {},
        )
        for t in runtime.tools
    ]
