"""
Generic MCP agent client
========================
Connects to one or more MCP servers (declared in a servers.json mapping),
lists their tools, and drives an Ollama tool-calling loop until the model
produces a final answer or `max_iterations` is reached.

Built on top of packages that already implement the hard parts:
  - fastmcp.Client: MCP protocol, transport handling, and multi-server
    aggregation (tools come back namespaced as `{server}_{tool}`).
  - ollama.Client: native tool-calling chat API.

This is the piece meant to be reused across apps as-is — the parts you'll
usually customize live in AgentConfig (client/config.py): system_prompt,
servers.json, max_iterations, model.

Usage
-----
    from backend.apps.client.agent import MCPAgent
    from backend.apps.client.config import AgentConfig

    config = AgentConfig.load()  # reads client/servers.json + .env
    async with MCPAgent(config) as agent:
        answer = await agent.run("What does client/agent.py do?")
"""
from __future__ import annotations

from typing import Any

import ollama
from fastmcp import Client

from backend.apps.client.config import AgentConfig
from backend.apps.client.logging_config import setup_logging
from backend.apps.client.memory import ConversationMemory


class MCPAgent:
    """Tool-using chat agent over any number of MCP servers, backed by Ollama."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.logger = setup_logging(config.log_level)
        self.memory = ConversationMemory(config.system_prompt, config.max_history_messages)

        self._mcp: Client | None = None
        self._ollama = ollama.Client(host=config.ollama_base_url)
        self._tools: list[dict[str, Any]] = []  # ollama-format tool specs

    # ── Async context manager ────────────────────────────────────────────────

    async def __aenter__(self) -> "MCPAgent":
        self._mcp = Client(self.config.servers)
        await self._mcp.__aenter__()
        await self._load_tools()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._mcp:
            await self._mcp.__aexit__(*args)

    # ── Setup ─────────────────────────────────────────────────────────────────

    async def _load_tools(self) -> None:
        mcp_tools = await self._mcp.list_tools()
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema,
                },
            }
            for t in mcp_tools
        ]
        names = ", ".join(t.name for t in mcp_tools) or "(none)"
        self.logger.info(f"[bold cyan]Connected.[/bold cyan] {len(mcp_tools)} tool(s) available: {names}")

    @property
    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self._tools]

    # ── Core loop ─────────────────────────────────────────────────────────────

    async def run(self, question: str) -> str:
        """Answer one question, using tools as needed. Conversation persists in memory."""
        self.memory.add_user(question)
        self.logger.info(f"[bold]User:[/bold] {question}")

        for iteration in range(1, self.config.max_iterations + 1):
            self.logger.debug(f"Iteration {iteration}/{self.config.max_iterations}")

            response = self._ollama.chat(
                model=self.config.ollama_model,
                messages=self.memory.messages(),
                tools=self._tools or None,
                options={"temperature": self.config.temperature},
            )
            message = response.message

            if not message.tool_calls:
                # Got an answer without tool calls
                answer = message.content or ""
                self.memory.add_assistant(answer)
                self.logger.info(f"[bold green]Answer:[/bold green] {answer or '(empty)'}")

                # If answer is empty, try one more time to force a response
                if not answer and iteration < self.config.max_iterations:
                    self.logger.warning("Empty response from LLM, retrying...")
                    continue

                return answer

            self.memory.add_assistant(
                message.content,
                tool_calls=[tc.model_dump() for tc in message.tool_calls],
            )

            for call in message.tool_calls:
                await self._execute_tool_call(call)

        # Iteration budget exhausted: ask once more, tools disabled, to force an answer.
        self.logger.warning(f"Max iterations ({self.config.max_iterations}) reached; forcing a final answer.")
        response = self._ollama.chat(
            model=self.config.ollama_model,
            messages=self.memory.messages(),
            options={"temperature": self.config.temperature},
        )
        final = response.message.content or ""

        # If still empty, provide a summary from the conversation
        if not final:
            self.logger.warning("Final response is empty, attempting to synthesize from conversation.")
            final = self._synthesize_from_memory()

        self.memory.add_assistant(final)
        return final

    def _synthesize_from_memory(self) -> str:
        """Attempt to synthesize an answer from tool results in memory."""
        import json

        synthesis_parts = []

        for msg in self.memory._messages:
            if msg.get("role") == "tool":
                tool_name = msg.get('tool_name', 'unknown')
                content = msg.get('content', '')

                # Try to parse JSON results (from retrieval_retrieve_document, etc.)
                try:
                    data = json.loads(content)
                    if isinstance(data, dict) and 'source_chunks' in data:
                        chunks = data['source_chunks']
                        synthesis_parts.append(f"\n=== Results from {tool_name} ===")
                        for i, chunk in enumerate(chunks[:3], 1):  # Show top 3 chunks
                            text = chunk.get('text', '')[:150]
                            synthesis_parts.append(f"Chunk {i}: {text}...")
                except (json.JSONDecodeError, TypeError):
                    synthesis_parts.append(f"\n{tool_name}: {content[:150]}")

        if synthesis_parts:
            return "Based on retrieved document chunks:\n" + "\n".join(synthesis_parts)
        return "Unable to generate response from available information."

    async def _execute_tool_call(self, call: Any) -> None:
        name = call.function.name
        arguments = dict(call.function.arguments or {})
        self.logger.info(f"[bold yellow]Tool call:[/bold yellow] {name}({arguments})")

        try:
            result = await self._mcp.call_tool(name, arguments)

            # Extract text from result
            if isinstance(result, str):
                text = result
            else:
                content = getattr(result, "content", result)
                if isinstance(content, list):
                    text = "\n".join(b.text for b in content if hasattr(b, "text"))
                else:
                    text = str(content) if content else str(result)

            # Ensure we have text to add
            if not text:
                text = f"Tool {name} returned no content. Raw result: {str(result)[:200]}"

        except Exception as exc:  # noqa: BLE001 — surface any tool failure back to the model
            text = f"Error calling tool '{name}': {exc}"
            self.logger.error(text)

        self.logger.info(f"[bold cyan]Tool result[/bold cyan] ({name}): {text[:200]}")
        self.memory.add_tool_result(name, text)

    # ── Utilities ─────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear conversation history (tools/connection stay live)."""
        self.memory.clear()
