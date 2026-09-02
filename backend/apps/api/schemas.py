"""Pydantic request/response models for the API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class CreateSessionRequest(BaseModel):
    system_prompt: str | None = None
    """Override the default agent system prompt for this session."""

    approval_required_tools: list[str] | None = None
    """
    Tool names that must pause for human approval in this session.
    Pass ["*"] to require approval for every tool call.
    Omit to fall back to the server-wide default (API_APPROVAL_REQUIRED_TOOLS).
    Pass [] explicitly to disable human-in-the-loop for this session.
    """


class SessionResponse(BaseModel):
    session_id: str
    tool_names: list[str]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class InterruptDecisionRequest(BaseModel):
    action: Literal["approve", "reject", "edit"]
    arguments: dict[str, Any] | None = None
    """Replacement arguments; only used when action == 'edit'."""
    reason: str | None = None
    """Optional human-readable note, surfaced back to the model on reject."""


class InterruptDecisionResponse(BaseModel):
    interrupt_id: str
    accepted: bool
