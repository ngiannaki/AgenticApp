"""
CLI for the generic MCP agent client.

    python -m client "What does client/agent.py do?"     # one-shot
    python -m client                                       # interactive chat
    python -m client --servers path/to/servers.json --model qwen2.5 "..."
"""
from __future__ import annotations

import asyncio

import typer
from rich.console import Console

from client.agent import MCPAgent
from client.config import DEFAULT_SERVERS_PATH, AgentConfig

app = typer.Typer(add_completion=False)
console = Console()


@app.command()
def main(
    question: str = typer.Argument(None, help="One-shot question. Omit for interactive chat."),
    servers: str = typer.Option(str(DEFAULT_SERVERS_PATH), help="Path to servers.json mapping."),
    model: str = typer.Option(None, help="Ollama model override."),
    max_iterations: int = typer.Option(8, help="Max tool-call round-trips per query."),
    log_level: str = typer.Option("INFO", help="DEBUG, INFO, WARNING, ERROR."),
) -> None:
    overrides = {"max_iterations": max_iterations, "log_level": log_level}
    if model:
        overrides["ollama_model"] = model
    config = AgentConfig.load(servers_path=servers, **overrides)

    if question:
        asyncio.run(_one_shot(config, question))
    else:
        asyncio.run(_chat(config))


async def _one_shot(config: AgentConfig, question: str) -> None:
    async with MCPAgent(config) as agent:
        await agent.run(question)


async def _chat(config: AgentConfig) -> None:
    async with MCPAgent(config) as agent:
        console.print(
            f"[dim]Connected to {len(agent.tool_names)} tool(s). "
            "Type 'exit' to quit, 'reset' to clear history.[/dim]\n"
        )
        while True:
            try:
                question = console.input("[bold]> [/bold]").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not question:
                continue
            if question.lower() in ("exit", "quit"):
                break
            if question.lower() == "reset":
                agent.reset()
                console.print("[dim]History cleared.[/dim]")
                continue
            await agent.run(question)


if __name__ == "__main__":
    app()
