"""Back-compat re-export. The Code Execution MCP server's settings live in
`backend/config/settings.py` (part of the single-source-of-truth config
module) — import from `backend.config` in new code.
"""
from __future__ import annotations

from backend.config.settings import CodeMcpSettings as Settings
from backend.config.settings import get_code_mcp_settings as get_settings

__all__ = ["Settings", "get_settings"]
