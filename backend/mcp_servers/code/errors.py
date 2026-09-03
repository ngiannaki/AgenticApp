"""Structured error types for the Code Execution MCP server.

Every tool returns a plain dict envelope (matching the rest of this repo's
MCP servers: ``{"success": bool, ...}``). These exceptions are the internal
vocabulary tool implementations raise; ``to_error_dict`` turns them into the
envelope without ever leaking paths, environment contents, or interpreter
internals.
"""

from __future__ import annotations

from typing import Any, Optional


class CodeServerError(Exception):
    """Base class for all errors raised inside the code execution MCP server."""

    category = "execution_error"

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(CodeServerError):
    """Malformed or disallowed input (empty code, code/command too long, bad timeout)."""

    category = "validation_error"


class TimeoutError_(CodeServerError):
    """Execution exceeded the configured timeout and was terminated."""

    category = "timeout_error"


class UnsupportedOperationError(CodeServerError):
    """The requested operation is disabled or not permitted by configuration."""

    category = "unsupported_operation"


def to_error_dict(exc: Exception) -> dict[str, Any]:
    """Convert any exception into the tool-facing structured error envelope."""

    if isinstance(exc, CodeServerError):
        return {
            "success": False,
            "error": exc.message,
            "error_category": exc.category,
            **({"details": exc.details} if exc.details else {}),
        }

    # Unexpected/unknown exception: never leak internals to the agent.
    return {
        "success": False,
        "error": "An internal code execution error occurred.",
        "error_category": "execution_error",
    }
