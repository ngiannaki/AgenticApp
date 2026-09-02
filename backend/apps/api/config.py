"""Settings for the FastAPI SSE backend.

Server connectivity for the MCP agent is loaded from the same servers.json
used by the CLI client (backend/apps/client/servers.json) unless overridden.
API-specific knobs (CORS, ports, human-in-the-loop defaults) come from
environment variables / backend/.env with the API_ prefix.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_SERVERS_PATH = _BACKEND_DIR / "apps" / "client" / "servers.json"


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        env_prefix="API_",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8100

    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000"],
        description="Origins allowed to call the API (the Next.js frontend).",
    )

    servers_path: Path = DEFAULT_SERVERS_PATH
    """Path to the FastMCP-style servers.json mapping used by example_mcp_client.py."""

    max_iterations: int = 15
    temperature: float = 0.3
    max_history_messages: int = 40

    log_level: str = "INFO"

    approval_required_tools: list[str] = Field(
        default_factory=list,
        description=(
            "Tool names that always pause for human approval before executing. "
            "Use ['*'] to require approval for every tool call by default. "
            "Sessions may override this at creation time."
        ),
    )

    interrupt_heartbeat_seconds: float = 15.0
    """How often to send an SSE keep-alive comment while waiting on a human decision."""

    interrupt_timeout_seconds: float | None = None
    """Optional hard timeout for a pending human-in-the-loop decision. None = wait forever."""

    session_idle_timeout_seconds: float = 3600.0
    """Sessions untouched for this long are evicted by the periodic cleanup task."""

    mongo_url: str = "mongodb://localhost:27017"
    """MongoDB connection string used to durably store conversations/messages."""

    mongo_db_name: str = "agentic_chat"
    """MongoDB database name for conversation persistence."""


_settings: ApiSettings | None = None


def get_api_settings() -> ApiSettings:
    global _settings
    if _settings is None:
        _settings = ApiSettings()
    return _settings
