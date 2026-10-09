"""The agent framework: write an agent as one class with one linear `run()`.

Adding an agent (see research.py / analyst.py for real examples):

    class MyAgent(BaseAgent):
        id = "my_agent"
        name = "My agent"
        description = "What it does, shown in the UI and the A2A agent card."
        inputs = [Field("question", "textarea", "What should I do?", required=True)]
        allowed_tools = {"retrieval_search_my_documents_v2"}

        async def run(self, ctx: RunContext, inputs: dict) -> None:
            await ctx.step("plan", "Thinking")
            answer = await ctx.ask("Which year?")          # pauses the task until the user replies
            result = await ctx.call_tool("retrieval_search_my_documents_v2", {"query": answer})
            await ctx.artifact("report.md", text="# Done")  # deliverables

    ...then add it to AGENTS in registry.py. That's all: the A2A server, its
    agent card, the API's task endpoints and the frontend page pick it up
    from the registry (the UI renders `inputs` as a form).

How a run maps onto A2A: the first message starts `run()` as a background
coroutine; this module forwards its steps/artifacts as A2A status/artifact
events. `ctx.ask()` publishes INPUT_REQUIRED and the A2A `execute()` call
returns, as the protocol expects; when the user's reply arrives (a new
`execute()` on the same task) the waiting `ask()` resumes with it. So agent
authors write straight-line code and never see the protocol.

Tools are not called directly: `ctx.call_tool` goes through the API's
internal tool endpoint, so the same ownership checks, approvals, sandbox
file tokens and artifact handling as in chat apply (backend/apps/api/routers/internal.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from a2a.helpers.proto_helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState

from backend.config import get_async_openai_client, get_chat_extra_body, settings
from backend.core.observability import estimate_usage, usage_from_response

logger = logging.getLogger("agents")


@dataclass
class Field:
    """One input of an agent, rendered as a form field by the frontend.

    kind: text | textarea | boolean | select | documents
    (`documents` = pick from the user's uploaded documents; `file_types`
    narrows it, e.g. ["csv", "xlsx"]).
    """

    name: str
    kind: str
    label: str
    required: bool = False
    default: Any = None
    options: list[str] = field(default_factory=list)
    help: str = ""
    file_types: list[str] = field(default_factory=list)


class AgentError(Exception):
    """Raised by an agent to fail its task with a user-facing message."""


@dataclass
class ToolResult:
    ok: bool
    text: str

    @property
    def data(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            return None


class BaseAgent:
    id: str = ""
    name: str = ""
    description: str = ""
    inputs: list[Field] = []
    allowed_tools: set[str] = set()
    examples: list[str] = []

    async def run(self, ctx: "RunContext", inputs: dict[str, Any]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        """The JSON the API and frontend use (GET /agents)."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "inputs": [asdict(f) for f in self.inputs],
            "examples": self.examples,
        }

    def validate(self, inputs: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for f in self.inputs:
            value = inputs.get(f.name, f.default)
            if f.required and value in (None, "", []):
                raise AgentError(f"'{f.label}' is required.")
            if f.kind == "boolean":
                value = bool(value)
            elif f.kind == "documents":
                value = [str(v) for v in (value or [])]
            elif f.kind == "select" and value not in f.options:
                value = f.default
            elif value is not None:
                value = str(value)
            clean[f.name] = value
        return clean


# --- LLM helpers ------------------------------------------------------------


def parse_json(text: str) -> Any:
    """Tolerant JSON extraction from an LLM reply (code fences, prose around it)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if match:
            try:
                return json.loads(match.group(1))
            except ValueError:
                pass
    return None


class Llm:
    """Thin wrapper over the app's one OpenAI-compatible client (Ollama in
    DEV, OpenAI in PROD — same switch as the chat)."""

    def __init__(self, user_id: str = "", task_id: str = "", agent_id: str = "") -> None:
        self._client = None
        self.user_id, self.task_id, self.agent_id = user_id, task_id, agent_id
        self._reports: set[asyncio.Task] = set()

    @property
    def client(self):
        if self._client is None:
            self._client = get_async_openai_client()
        return self._client

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.2):
        started = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=settings.chat_model,
                messages=messages,
                tools=tools or None,
                temperature=temperature,
                extra_body=get_chat_extra_body(),
            )
        except Exception as exc:
            self._report(started, error=str(exc))
            raise
        message = response.choices[0].message
        usage = usage_from_response(getattr(response, "usage", None))
        if usage is None:  # server reported none: estimate, flagged as such
            produced = (message.content or "") + "".join(tc.function.arguments or "" for tc in (message.tool_calls or []))
            usage = estimate_usage(messages, produced)
        self._report(started, usage=usage)
        return message

    def _report(self, started: float, usage=None, error: str | None = None) -> None:
        """Tell the API about this model call (it owns the database), without
        slowing the agent down; a failure to report is ignored."""
        if not self.user_id or not settings.agents.internal_token:
            return
        payload = {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "model": settings.chat_model,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "status": "error" if error else "ok",
            "error": error,
            **({"input_tokens": usage.input_tokens, "cached_tokens": usage.cached_tokens, "output_tokens": usage.output_tokens,
                "reasoning_tokens": usage.reasoning_tokens, "estimated": usage.estimated} if usage else {}),
        }

        async def send() -> None:
            try:
                async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                    await client.post(
                        f"{settings.agents.api_url}/internal/usage",
                        json=payload,
                        headers={"Authorization": f"Bearer {settings.agents.internal_token}"},
                    )
            except Exception:  # noqa: BLE001 - tracking must never break a task
                logger.debug("Could not report model usage", exc_info=True)

        task = asyncio.get_running_loop().create_task(send())
        self._reports.add(task)
        task.add_done_callback(self._reports.discard)

    async def text(self, system: str, user: str) -> str:
        message = await self.complete([{"role": "system", "content": system}, {"role": "user", "content": user}])
        return message.content or ""

    async def json(self, system: str, user: str, retries: int = 1) -> Any:
        prompt = user + "\n\nReply with JSON only."
        for _ in range(retries + 1):
            data = parse_json(await self.text(system, prompt))
            if data is not None:
                return data
        raise AgentError("The model didn't return valid JSON.")


# --- the run context ----------------------------------------------------------


class ToolClient:
    """Calls tools through the API's internal endpoint (as the task's user)."""

    def __init__(self, user_id: str, agent_id: str, task_id: str = "") -> None:
        self.user_id = user_id
        self.agent_id = agent_id
        self.task_id = task_id

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.agents.internal_token}"}

    async def call(self, name: str, arguments: dict[str, Any], approved: bool = False) -> ToolResult:
        payload = {"user_id": self.user_id, "agent_id": self.agent_id, "task_id": self.task_id, "name": name, "arguments": arguments, "approved": approved}
        async with httpx.AsyncClient(timeout=settings.agents.tool_call_timeout_seconds + 30, trust_env=False) as client:
            response = await client.post(f"{settings.agents.api_url}/internal/tools/call", json=payload, headers=self._headers())
        if response.status_code != 200:
            return ToolResult(False, f"Tool call failed (HTTP {response.status_code}): {response.text[:300]}")
        body = response.json()
        return ToolResult(bool(body.get("ok")), str(body.get("text", "")))

    async def schemas(self, names: set[str]) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.get(
                f"{settings.agents.api_url}/internal/tools", params={"names": ",".join(sorted(names))}, headers=self._headers()
            )
        response.raise_for_status()
        return response.json()


class RunContext:
    """What an agent's run() gets: progress, questions, tools, deliverables."""

    def __init__(self, agent: BaseAgent, task_id: str, user_id: str, tools: ToolClient | None = None, llm: Llm | None = None):
        self.agent = agent
        self.task_id = task_id
        self.user_id = user_id
        self.tools = tools or ToolClient(user_id, agent.id, task_id)
        self.llm = llm or Llm(user_id=user_id, task_id=task_id, agent_id=agent.id)
        self._updater: TaskUpdater | None = None
        self._paused = asyncio.Event()
        self._reply: asyncio.Future[str] | None = None

    # wiring (used by AgentExecutorBridge) ----------------------------------
    def attach(self, updater: TaskUpdater) -> None:
        self._updater = updater

    # API for agents -----------------------------------------------------
    async def step(self, kind: str, title: str, detail: str = "", data: Any = None) -> None:
        """A timeline entry: kind is a free label the UI styles (plan, search,
        read, code, result, note...)."""
        meta = {"event": "step", "kind": kind, "title": title, "detail": detail[:20000]}
        if data is not None:
            meta["data_json"] = json.dumps(data, default=str)[:50000]
        await self._updater.update_status(
            TaskState.TASK_STATE_WORKING, self._updater.new_agent_message([new_text_part(title)]), metadata=meta
        )

    async def ask(self, question: str, options: list[str] | None = None) -> str:
        """Pause the task until the user answers; returns their answer."""
        loop = asyncio.get_running_loop()
        self._reply = loop.create_future()
        await self._updater.update_status(
            TaskState.TASK_STATE_INPUT_REQUIRED,
            self._updater.new_agent_message([new_text_part(question)]),
            metadata={"event": "question", "question": question, "options_json": json.dumps(options or [])},
        )
        self._paused.set()
        answer = await self._reply
        self._reply = None
        return answer

    async def artifact(self, name: str, text: str | None = None, data: Any = None, kind: str = "markdown") -> None:
        """A deliverable. kind: markdown | files | sources | table | json."""
        parts = []
        if text is not None:
            parts.append(new_text_part(text))
        if data is not None:
            parts.append(new_text_part(json.dumps(data, default=str)))
        await self._updater.add_artifact(parts, name=name, metadata={"kind": kind, "has_data": data is not None})

    async def call_tool(self, name: str, arguments: dict[str, Any], approved: bool = False) -> ToolResult:
        if name not in self.agent.allowed_tools:
            return ToolResult(False, f"Tool {name!r} is not available to this agent.")
        return await self.tools.call(name, arguments, approved=approved)

    async def tool_loop(
        self,
        system: str,
        user: str,
        tool_names: set[str],
        local_tools: dict[str, tuple[dict, Any]] | None = None,
        max_iterations: int = 12,
        on_call=None,
        confirm: set[str] | None = None,
    ) -> str:
        """A generic LLM tool-calling loop over API tools (+ optional local
        tools: name -> (openai tool schema, async handler(args) -> str)).
        Tools in `confirm` ask the user before running (and then run as
        approved). Returns the model's final text."""
        local_tools = local_tools or {}
        schemas = await self.tools.schemas(tool_names) + [schema for schema, _ in local_tools.values()]
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(max_iterations):
            message = await self.llm.complete(messages, tools=schemas)
            if not message.tool_calls:
                return message.content or ""
            messages.append({"role": "assistant", "content": message.content, "tool_calls": [tc.model_dump() for tc in message.tool_calls]})
            for call in message.tool_calls:
                args = parse_json(call.function.arguments or "{}") or {}
                name = call.function.name
                if name in local_tools:
                    text = str(await local_tools[name][1](args))
                    ok = True
                elif name not in tool_names:
                    text, ok = f"Tool {name!r} is not available here.", False
                elif confirm and name in confirm:
                    summary = json.dumps(args, default=str)[:600]
                    answer = await self.ask(f"Allow the agent to run {name} with {summary}?", ["Approve", "Reject"])
                    if answer.strip().lower().startswith("approve"):
                        result = await self.call_tool(name, args, approved=True)
                        text, ok = result.text, result.ok
                    else:
                        text, ok = "The user declined this action. Do not retry it; continue without it.", False
                else:
                    result = await self.call_tool(name, args)
                    text, ok = result.text, result.ok
                if on_call:
                    await on_call(name, args, ok, text)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text[:12000]})
        messages.append({"role": "user", "content": "Stop calling tools now and give your final answer."})
        return (await self.llm.complete(messages)).content or ""


# --- A2A executor bridge --------------------------------------------------------


class AgentExecutorBridge(AgentExecutor):
    """Runs a BaseAgent behind the A2A protocol (see the module docstring)."""

    def __init__(self, agent: BaseAgent) -> None:
        self.agent = agent
        self._runs: dict[str, tuple[RunContext, asyncio.Task]] = {}

    async def execute(self, context: RequestContext, event_queue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        existing = self._runs.get(task.id)
        if existing is not None:
            ctx, runner = existing
            ctx.attach(updater)
            ctx._paused.clear()
            if ctx._reply is not None and not ctx._reply.done():
                ctx._reply.set_result(context.get_user_input())
            await self._pump(task.id, ctx, runner)
            return

        meta = dict(context.message.metadata) if context.message and context.message.metadata else {}
        user_id = str(meta.get("user_id") or "")
        try:
            inputs = json.loads(context.get_user_input() or "{}")
        except ValueError:
            inputs = {}
        await updater.start_work()
        if not user_id:
            await updater.failed(updater.new_agent_message([new_text_part("Missing user.")]))
            return
        try:
            inputs = self.agent.validate(inputs)
        except AgentError as exc:
            await updater.failed(updater.new_agent_message([new_text_part(str(exc))]))
            return

        ctx = RunContext(self.agent, task.id, user_id)
        ctx.attach(updater)
        runner = asyncio.create_task(self._run(ctx, inputs))
        self._runs[task.id] = (ctx, runner)
        await self._pump(task.id, ctx, runner)

    async def _run(self, ctx: RunContext, inputs: dict[str, Any]) -> None:
        try:
            await self.agent.run(ctx, inputs)
            await ctx._updater.complete()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure ends the task with a message
            logger.exception("Agent %s failed on task %s", self.agent.id, ctx.task_id)
            message = str(exc) if isinstance(exc, AgentError) else f"The agent failed: {exc}"
            await ctx._updater.failed(ctx._updater.new_agent_message([new_text_part(message)]))

    async def _pump(self, task_id: str, ctx: RunContext, runner: asyncio.Task) -> None:
        """Return when the run finishes or pauses on ask()."""
        paused = asyncio.create_task(ctx._paused.wait())
        await asyncio.wait({runner, paused}, return_when=asyncio.FIRST_COMPLETED)
        paused.cancel()
        if runner.done():
            self._runs.pop(task_id, None)

    async def cancel(self, context: RequestContext, event_queue) -> None:
        entry = self._runs.pop(context.task_id or "", None)
        if entry:
            entry[1].cancel()
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
