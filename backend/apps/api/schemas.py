"""Pydantic request/response models for the API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SignupRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=8)
    invite_code: str = ""


class LoginRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=1)


class AuthResponse(BaseModel):
    user_id: str
    email: str
    token: str


class CurrentUserResponse(BaseModel):
    user_id: str
    email: str


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class CreateSessionRequest(BaseModel):
    # No system_prompt override: the system prompt carries the agent's tool
    # and safety rules, so it's server-controlled only. (An old client still
    # sending the field is fine — unknown fields are ignored.)

    approval_required_tools: list[str] | None = None
    """
    Extra tool names that must pause for human approval in this session.
    Pass ["*"] to require approval for every tool call.
    Omit to fall back to the server-wide default (API_APPROVAL_REQUIRED_TOOLS).
    Pass [] to add nothing beyond API_MANDATORY_APPROVAL_TOOLS, which always
    apply and can't be turned off from here (see ChatSession.requires_approval).
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


class QuestionAnswerRequest(BaseModel):
    answer: str = Field(min_length=1)
    """The user's answer — either the text of the option button they picked, or free text they typed."""


class QuestionAnswerResponse(BaseModel):
    question_id: str
    accepted: bool
