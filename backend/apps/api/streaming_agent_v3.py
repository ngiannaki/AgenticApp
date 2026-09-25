"""Third streaming agent: the same orchestrator / executor / critic
architecture as streaming_agent_v2.py, but built on the openai-agents SDK's
native Agent/Runner/handoff primitives instead of a hand-rolled loop over
chat.completions. See streaming_agent_v2.py's module docstring for why the
architecture looks the way it does; this file is the "real handoff API"
implementation of the same idea.

Three Agent objects, wired into a cycle with the SDK's handoff():

    orchestrator --handoff--> executor --handoff--> critic --handoff--> executor (loop)

  - orchestrator: no tools. Plans (optionally writing the plan as its
    message text, which we surface as a "plan" event) then hands off to the
    executor with the request or first step.
  - executor: every MCP tool plus ask_user, wrapped as SDK FunctionTools.
    Carries out the step, then must hand off to the critic with a summary
    (instructed never to answer the user directly itself).
  - critic: no tools. Either responds with the final answer directly (which
    ends the whole run — the SDK treats a tool/handoff-free message as the
    run's final_output) or hands back to the executor with one concrete
    follow-up instruction, closing the loop.

Runner.run_streamed's `max_turns` is what bounds that loop (mirrors
AgentRuntime's max_iterations budget in streaming_agent.py) instead of an
explicit critic-round counter.

The model is pointed at the same shared openai.AsyncOpenAI client
(backend.config.get_async_openai_client) via OpenAIChatCompletionsModel, so
this still talks to Ollama's OpenAI-compatible /v1 endpoint in DEV and real
OpenAI in PROD exactly like everything else in this backend — the
openai-agents SDK's default Responses-API model is never used here.

Human-in-the-loop interrupts and the ask_user tool are implemented by hand
inside each tool's on_invoke_tool (pausing on the same session-owned
asyncio.Future the REST resume endpoints resolve — see sessions.py), not
via the SDK's own ToolApprovalItem/approval-resume machinery: that machinery
is built around pausing and later resuming a run across process/request
boundaries with a serialized RunState, which doesn't fit this codebase's
single-long-lived-SSE-request model as directly as reusing the same
Future-based pause streaming_agent.py already relies on.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from agents import Agent, FunctionTool, ModelSettings, RunConfig, Runner, handoff
from agents.items import ItemHelpers
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.stream_events import AgentUpdatedStreamEvent, RunItemStreamEvent, StreamEvent
from agents.tool_context import ToolContext

from backend.apps.api import events
from backend.apps.api.config import ApiSettings
from backend.apps.api.sessions import ChatSession
from backend.apps.api.streaming_agent import (
    _ASK_USER_TOOL_SCHEMA,
    ASK_USER_TOOL_NAME,
    AgentRuntime,
    _extract_text,
    _with_attached_documents_note,
)
from backend.config import get_chat_extra_body, settings as app_settings

logger = logging.getLogger("agentic_api")

_ORCHESTRATOR_INSTRUCTIONS = (
    "You are the orchestrator. Read the user's request and decide how to "
    "hand it to the executor agent. For a simple request, hand off to the "
    "executor with the request essentially as-is. For a request that "
    "benefits from being split into steps, first write a short numbered "
    "plan as your plain-text reply, then in the same turn hand off to the "
    "executor with that plan and the first step to start on — the executor "
    "and critic will come back to you for further rounds if more steps "
    "remain. You have no tools of your own and must never answer the user "
    "directly: your only job is to plan and hand off to the executor."
)

_EXECUTOR_INSTRUCTIONS = (
    "You are the executor. Use tools to carry out the instructions you were "
    "handed off with. Only call a tool when it's actually needed; never "
    "call one speculatively or with guessed/placeholder arguments — if "
    "you're missing information only the user can supply, use the ask_user "
    "tool instead of guessing. When you are done with your tool work for "
    "this handoff, you MUST hand off to the critic with a concise, factual "
    "summary of what you did and what you found. Never answer the user "
    "directly yourself — the critic decides whether the work is complete."
)

_CRITIC_INSTRUCTIONS = (
    "You are the critic. You receive a summary of work the executor has "
    "done toward the user's original request (visible earlier in this "
    "conversation). If it fully and correctly answers the request, reply "
    "directly with the final natural-language answer for the user — plain "
    "text, no mention of steps, executors, plans, or the review process. "
    "That reply ends the task. If the work is incomplete or wrong, instead "
    "hand off back to the executor with one concrete, specific follow-up "
    "instruction describing exactly what is still missing — do not hand off "
    "without a clear instruction, and do not loop back for polish once the "
    "request is genuinely answered."
)


def _tool_call_name_and_args(raw_item: Any) -> tuple[str, str]:
    if isinstance(raw_item, dict):
        return raw_item.get("name", "?"), raw_item.get("arguments") or "{}"
    return getattr(raw_item, "name", "?"), getattr(raw_item, "arguments", None) or "{}"


class OrchestratedAgentRuntime:
    """Same public surface as streaming_agent.AgentRuntime (connect / close /
    tools / tool_names / run_stream) so it's a drop-in alternative — see the
    module docstring for the openai-agents-SDK-based planner/executor/critic
    loop it runs instead of a single flat tool-calling loop."""

    def __init__(self, base: AgentRuntime) -> None:
        self.base = base

    @classmethod
    async def connect(cls, servers: dict, settings: ApiSettings) -> "OrchestratedAgentRuntime":
        return cls(await AgentRuntime.connect(servers, settings))

    async def close(self) -> None:
        await self.base.close()

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self.base.tools

    @property
    def tool_names(self) -> list[str]:
        return self.base.tool_names

    @property
    def settings(self) -> ApiSettings:
        return self.base.settings

    def _build_mcp_tool(self, tool_def: dict[str, Any], session: ChatSession, queue: "asyncio.Queue[Any]") -> FunctionTool:
        name = tool_def["function"]["name"]
        description = tool_def["function"]["description"]
        schema = tool_def["function"]["parameters"] or {"type": "object", "properties": {}}

        async def on_invoke_tool(ctx: ToolContext[Any], args_json: str) -> str:
            arguments = json.loads(args_json) if args_json else {}
            call_id = ctx.tool_call_id

            if session.requires_approval(name):
                pending = session.new_interrupt(call_id, name, arguments)
                await queue.put(events.interrupt(pending.interrupt_id, call_id, name, arguments))
                decision = await self.base._await_decision(pending.future)
                session.pending_interrupts.pop(pending.interrupt_id, None)
                if decision.action == "reject":
                    text = f"Tool call '{name}' was rejected by the user."
                    if decision.reason:
                        text += f" Reason: {decision.reason}"
                    return text
                if decision.action == "edit" and decision.arguments is not None:
                    arguments = decision.arguments

            # Derived from this tool's own schema (see AgentRuntime.user_scoped_tool_names
            # in streaming_agent.py) rather than a hardcoded name list — `schema` is this
            # tool's own inputSchema, already in closure scope from _build_mcp_tool below.
            if "user_id" in (schema.get("properties") or {}):
                arguments["user_id"] = session.user_id

            await queue.put(events.status(f"calling {name}"))
            try:
                result = await self.base.mcp.call_tool(name, arguments)
                return _extract_text(result)
            except Exception as exc:  # noqa: BLE001 — surface any tool failure back to the model
                logger.error("Tool '%s' failed: %s", name, exc)
                return f"Error calling tool '{name}': {exc}"

        return FunctionTool(
            name=name,
            description=description,
            params_json_schema=schema,
            on_invoke_tool=on_invoke_tool,
            strict_json_schema=False,
        )

    def _build_ask_user_tool(self, session: ChatSession, queue: "asyncio.Queue[Any]") -> FunctionTool:
        schema = _ASK_USER_TOOL_SCHEMA["function"]["parameters"]

        async def on_invoke_tool(ctx: ToolContext[Any], args_json: str) -> str:
            arguments = json.loads(args_json) if args_json else {}
            call_id = ctx.tool_call_id
            prompt = str(arguments.get("prompt") or "").strip()
            options = [str(o) for o in (arguments.get("options") or []) if str(o).strip()]
            if not prompt:
                return "Error calling tool 'ask_user': 'prompt' is required."

            pending = session.new_question(call_id, prompt, options, allow_other=True)
            await queue.put(events.question(pending.question_id, call_id, prompt, options, allow_other=True))
            answer = await self.base._await_answer(pending.future)
            session.pending_questions.pop(pending.question_id, None)
            return answer.answer

        return FunctionTool(
            name=ASK_USER_TOOL_NAME,
            description=_ASK_USER_TOOL_SCHEMA["function"]["description"],
            params_json_schema=schema,
            on_invoke_tool=on_invoke_tool,
            strict_json_schema=False,
        )

    def _build_orchestrator(self, session: ChatSession, queue: "asyncio.Queue[Any]") -> Agent:
        tools = [self._build_ask_user_tool(session, queue)] + [
            self._build_mcp_tool(t, session, queue)
            for t in self.base.tools
            if t["function"]["name"] != ASK_USER_TOOL_NAME
        ]

        executor = Agent(name="executor", instructions=_EXECUTOR_INSTRUCTIONS, tools=tools)
        critic = Agent(name="critic", instructions=_CRITIC_INSTRUCTIONS)
        orchestrator = Agent(name="orchestrator", instructions=_ORCHESTRATOR_INSTRUCTIONS, handoffs=[handoff(executor)])
        executor.handoffs = [handoff(critic)]
        critic.handoffs = [handoff(executor)]
        return orchestrator

    async def run_stream(self, session: ChatSession, question: str) -> AsyncIterator[dict[str, Any]]:
        session.touch()
        full_question = _with_attached_documents_note(session, question)

        queue: "asyncio.Queue[Any]" = asyncio.Queue()
        call_names: dict[str, str] = {}
        state: dict[str, Any] = {"answer": None, "turns": 0}

        def translate(ev: StreamEvent) -> list[dict[str, Any]]:
            if isinstance(ev, AgentUpdatedStreamEvent):
                return [events.status(f"now active: {ev.new_agent.name}")]
            if not isinstance(ev, RunItemStreamEvent):
                return []
            item = ev.item
            agent_name = getattr(getattr(item, "agent", None), "name", "?")

            if ev.name == "tool_called":
                call_id = getattr(item, "call_id", None) or "?"
                name, args_str = _tool_call_name_and_args(item.raw_item)
                call_names[call_id] = name
                try:
                    arguments = json.loads(args_str) if args_str else {}
                except json.JSONDecodeError:
                    arguments = {"_raw": args_str}
                return [events.tool_call(call_id, name, arguments)]

            if ev.name == "tool_output":
                call_id = getattr(item, "call_id", None) or "?"
                name = call_names.get(call_id, "?")
                output = getattr(item, "output", "")
                text = output if isinstance(output, str) else json.dumps(output, default=str)
                return [events.tool_result(call_id, name, True, text)]

            if ev.name == "handoff_occured":
                target = getattr(getattr(item, "target_agent", None), "name", "?")
                return [events.handoff(target, "")]

            if ev.name == "message_output_created":
                text = ItemHelpers.text_message_output(item)
                if not text:
                    return []
                if agent_name == "orchestrator":
                    return [events.plan([line.strip() for line in text.splitlines() if line.strip()] or [text])]
                return [events.status(f"[{agent_name}] {text}")]

            return []

        async def driver() -> None:
            try:
                orchestrator = self._build_orchestrator(session, queue)
                model = OpenAIChatCompletionsModel(model=app_settings.chat_model, openai_client=self.base.llm)
                run_config = RunConfig(
                    model=model,
                    model_settings=ModelSettings(
                        temperature=self.base.settings.temperature,
                        extra_body=get_chat_extra_body(),
                    ),
                    workflow_name="orchestrated-chat",
                )
                result = Runner.run_streamed(
                    orchestrator,
                    input=full_question,
                    max_turns=self.base.settings.max_iterations,
                    run_config=run_config,
                )
                async for ev in result.stream_events():
                    for sse in translate(ev):
                        await queue.put(sse)
                state["turns"] = result.current_turn
                state["answer"] = str(result.final_output or "")
            except Exception as exc:  # noqa: BLE001 — surface upstream failure and end the turn
                logger.exception("Orchestrated (agents SDK) run failed")
                await queue.put(events.error(f"agent run failed: {exc}"))
            finally:
                await queue.put(None)

        task = asyncio.create_task(driver())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            await task

        answer = state["answer"]
        if answer is None:
            return  # a driver-side error already produced an "error" event
        session.memory.add_user(full_question)
        session.memory.add_assistant(answer)
        yield events.message(answer)
        yield events.done(answer, state["turns"])
