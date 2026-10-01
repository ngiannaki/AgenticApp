"""Postgres access shared by the API and the indexing/retrieval MCP servers.

Sync psycopg 3 + a connection pool on purpose: the MCP servers are plain
synchronous FastMCP tools, and the async API reaches the same pool through
`run_sync` (a worker thread) — which also sidesteps psycopg's async driver
refusing Windows' default ProactorEventLoop when the API runs natively on
Windows (start_mcp_servers.ps1).
"""
from backend.core.db.migrate import apply_migrations, ensure_agent_role
from backend.core.db.pool import close_pool, get_pool, run_sync

__all__ = ["apply_migrations", "ensure_agent_role", "close_pool", "get_pool", "run_sync"]
