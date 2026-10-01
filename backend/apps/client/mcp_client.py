"""
Universal MCP Client
====================
Connects to any FastMCP server (stdio or HTTP) and provides a clean
interface for listing and calling tools.

Usage — from Python
-------------------
    import asyncio
    from client.mcp_client import MCPClient

    # HTTP server
    async with MCPClient.http("http://localhost:8001") as client:
        tools = await client.list_tools()
        result = await client.call_tool("analyze_code", {"code": "x = 1"})

    # stdio server (local script)
    async with MCPClient.stdio("servers/claude_server/server.py") as client:
        result = await client.call_tool("explain_code", {"code": "..."})

Usage — interactive REPL (CLI)
-------------------------------
    python client/mcp_client.py http://localhost:8001
    python client/mcp_client.py servers/claude_server/server.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from fastmcp import Client
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


class MCPClient:
    """Thin wrapper around fastmcp.Client with display and REPL utilities."""

    def __init__(self, target: str) -> None:
        """
        Args:
            target: A server script path (stdio) OR an HTTP URL.
        """
        self._target = target
        self._client: Client | None = None

    # ── Factory helpers ────────────────────────────────────────────────────────

    @classmethod
    def http(cls, url: str) -> "MCPClient":
        """Target an MCP server reachable over HTTP."""
        return cls(url)

    @classmethod
    def stdio(cls, script_path: str) -> "MCPClient":
        """Target a local MCP server script via stdio."""
        return cls(script_path)

    # ── Async context manager ──────────────────────────────────────────────────

    async def __aenter__(self) -> "MCPClient":
        self._client = Client(self._target)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.__aexit__(*args)

    # ── Core API ───────────────────────────────────────────────────────────────

    async def list_tools(self) -> list[dict]:
        """Return all tools exposed by the server."""
        tools = await self._client.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema,
            }
            for t in tools
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call a named tool and return its result as a string."""
        result = await self._client.call_tool(tool_name, arguments or {})
        # FastMCP 3.x returns a CallToolResult with a .content list;
        # earlier versions returned the list directly.
        content = getattr(result, "content", result)
        parts = [block.text for block in content if hasattr(block, "text")]
        return "\n".join(parts) if parts else str(result)

    # ── Display helpers ────────────────────────────────────────────────────────

    async def print_tools(self) -> None:
        """Pretty-print available tools to the terminal."""
        tools = await self.list_tools()
        table = Table(
            title=f"Tools  •  {self._target}",
            show_lines=True,
            header_style="bold cyan",
        )
        table.add_column("Tool", style="cyan", no_wrap=True)
        table.add_column("Description")
        for t in tools:
            table.add_row(t["name"], t["description"])
        console.print(table)

    # ── Interactive REPL ───────────────────────────────────────────────────────

    async def repl(self) -> None:
        """Interactive REPL for exploring an MCP server manually."""
        await self.print_tools()
        console.print(
            "\nType [cyan]<tool_name> <json_args>[/cyan]  "
            "or just [cyan]<tool_name>[/cyan] for no-arg tools.  "
            "[yellow]quit[/yellow] to exit.\n"
        )

        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("[dim]Exiting REPL.[/dim]")
                break

            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break

            parts = line.split(" ", 1)
            tool_name = parts[0]
            args_raw = parts[1] if len(parts) > 1 else "{}"

            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                console.print("[red]Arguments must be valid JSON, e.g. {\"code\": \"x=1\"}[/red]")
                continue

            try:
                result = await self.call_tool(tool_name, args)
                console.print(Panel(result, title=f"[green]{tool_name}[/green]"))
            except Exception as exc:
                console.print(f"[red]Error:[/red] {exc}")


# ── CLI entry point ────────────────────────────────────────────────────────────

async def _main(target: str) -> None:
    async with MCPClient(target) as client:
        await client.repl()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python client/mcp_client.py <server_url_or_script>")
        print("  HTTP : python client/mcp_client.py http://localhost:8001")
        print("  stdio: python client/mcp_client.py servers/claude_server/server.py")
        sys.exit(1)

    asyncio.run(_main(sys.argv[1]))
