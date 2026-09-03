"""Fast, non-executing syntax check for submitted Python code."""

from __future__ import annotations

import ast
from typing import Any

from backend.mcp_servers.code.errors import ValidationError


def check_python_syntax(code: str, *, settings: Any) -> dict[str, Any]:
    if not code or not code.strip():
        raise ValidationError("code must be a non-empty string.")
    if len(code) > settings.max_code_length:
        raise ValidationError(f"code exceeds the {settings.max_code_length}-character limit.")

    try:
        ast.parse(code, filename="<submitted_code>")
    except SyntaxError as exc:
        return {
            "success": False,
            "valid": False,
            "error": exc.msg,
            "line": exc.lineno,
            "column": exc.offset,
            "text": (exc.text or "").rstrip("\n"),
        }

    return {"success": True, "valid": True}
