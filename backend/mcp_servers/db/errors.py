"""Structured error types for the Database MCP server.

Every tool returns a plain dict envelope (matching the rest of this repo's
MCP servers: ``{"success": bool, ...}``). These exceptions are the internal
vocabulary tool implementations raise; ``to_error_dict`` turns them into the
envelope without ever leaking connection strings, stack traces, or paths.
"""

from __future__ import annotations

from typing import Any, Optional


class DBServerError(Exception):
    """Base class for all errors raised inside the database MCP server."""

    category = "database_error"

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(DBServerError):
    """Malformed or disallowed input (bad identifier, unknown column, bad type, ...)."""

    category = "validation_error"


class AuthorizationError(DBServerError):
    """A permission check failed (denied table/column/operation)."""

    category = "authorization_error"


class QueryError(DBServerError):
    """The SQL validator rejected a query, or the database rejected a valid-looking query."""

    category = "query_error"


class ConstraintError(DBServerError):
    """A database constraint (unique, foreign key, not-null, check) was violated."""

    category = "constraint_error"


class TimeoutError_(DBServerError):
    """A statement exceeded the configured execution timeout."""

    category = "timeout_error"


class UnsupportedOperationError(DBServerError):
    """The requested operation is not supported or is disabled by configuration."""

    category = "unsupported_operation"


def to_error_dict(exc: Exception) -> dict[str, Any]:
    """Convert any exception into the tool-facing structured error envelope."""

    if isinstance(exc, DBServerError):
        return {
            "success": False,
            "error": exc.message,
            "error_category": exc.category,
            **({"details": exc.details} if exc.details else {}),
        }

    # Unexpected/unknown exception: never leak internals to the agent.
    return {
        "success": False,
        "error": "An internal database error occurred.",
        "error_category": "database_error",
    }
