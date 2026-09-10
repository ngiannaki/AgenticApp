"""Second streaming agent: an orchestrator / executor / critic architecture,
built on the same plain openai.AsyncOpenAI chat-completions loop as
streaming_agent.py (no new dependency — works against Ollama in DEV exactly
like today). See streaming_agent_v3.py for the same three-role architecture
built on the openai-agents SDK's native Agent/Runner/handoff primitives
instead.

Where streaming_agent.AgentRuntime runs one flat tool-calling loop until the
model stops calling tools, this version reasons in three passes over the
same underlying LLM and MCP tools:

  1. Planner  - breaks the user's request into a short ordered list of
                concrete steps (falls back to a single step, the request
                itself, if planning produces nothing usable).
  2. Executor - runs each step as its own bounded tool-calling loop, isolated
                from the session's visible history in a scratch
                ConversationMemory so intermediate tool chatter never leaks
                into the user-visible conversation. This is the "handoff":
                the orchestrator hands the step's instructions to the
                executor and waits for it to hand back a result.
  3. Critic   - after all steps run, judges whether the accumulated results
                actually answer the user's original request. If not, it
                writes one concrete follow-up instruction and the
                orchestrator hands off another executor round (bounded by
                _MAX_CRITIC_ROUNDS) before finalizing the answer.

OrchestratedAgentRuntime wraps a plain streaming_agent.AgentRuntime by
composition (same MCP connection, tool list, and low-level
_chat/_handle_tool_call machinery) so interrupts, ask_user, and
user-scoped-tool injection all keep working exactly as they do today —
this file only adds the planning/handoff/critique loop on top.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator

from backend.apps.api import events
from backend.apps.api.config import ApiSettings
from backend.apps.api.sessions import ChatSession
from backend.apps.api.streaming_agent import AgentRuntime, _extract_reasoning, _with_attached_documents_note
from backend.apps.client.memory import ConversationMemory
from backend.config import get_ollama_extra_body, settings as app_settings

logger = logging.getLogger("agentic_api")

_MAX_PLAN_STEPS = 5
_MAX_CRITIC_ROUNDS = 2
_EXECUTOR_ITERATION_BUDGET = 6

_EXECUTOR_SYSTEM_PROMPT = (
    "You are an executor sub-agent working on one step of a larger plan. "
    "Use tools as needed to carry out the step described below, then reply "
    "with a concise, factual plain-text summary of what you did and what "
    "you found. That summary is handed back to the orchestrator, not shown "
    "to the user directly — be complete and factual rather than "
    "conversational. Only call a tool when it's actually needed; never call "
    "one speculatively or with guessed/placeholder arguments."
)

_PLANNER_INSTRUCTIONS = (
    "Break the following request into a short ordered plan of concrete, "
    "independently-executable steps. Most requests need only 1-3 steps; use "
    "more only when genuinely necessary — never pad the plan. Each step "
    "should be something a tool-using assistant could carry out on its own "
    "given only that step's text, so restate any needed context from the "
    "request rather than assuming it carries over between steps. Respond "
    "with ONLY a JSON array of step strings, e.g. [\"step one\", \"step "
    "two\"] — no prose, no markdown fences, nothing else.\n\nRequest: {question}"
)

_CRITIC_INSTRUCTIONS = (
    "You are reviewing whether the work done so far fully and correctly "
    "answers the user's original request. Respond with ONLY JSON of the "
    'shape {{"done": true|false, "feedback": "...", "followup": "..." or '
    'null}}. Set done=true only if the request is now fully answered by the '
    "work below. If it is not, set followup to a single concrete "
    "instruction describing the one additional executor step that would "
    "close the gap.\n\nOriginal request: {question}\n\nWork done so "
    "far:\n{work}"
)

_FINAL_INSTRUCTIONS = (
    "Write the final answer to the user's original request, using the work "
    "done so far. Answer directly and naturally, as if you did the work "
    "yourself — do not mention steps, plans, executors, or critics.\n\n"
    "Original request: {question}\n\nWork done so far:\n{work}"
)


def _parse_steps(text: str) -> list[str]:
    """Parse the planner's output into an ordered list of step instructions.

    Tries JSON first (a bare array of strings, or {"steps": [...]}); models
    that ignore the JSON-only instruction still produce something usable via
    a line-based fallback, one step per non-empty line with any "1.", "-",
    "*" list marker stripped.
    """
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("steps", [])
        if isinstance(data, list) and all(isinstance(s, str) for s in data):
            steps = [s.strip() for s in data if s.strip()]
            if steps:
                return steps
    except (json.JSONDecodeError, TypeError):
        pass

    steps = []
    for line in text.splitlines():
        line = re.sub(r"^\s*[-*]\s*|^\s*\d+[.)]\s*", "", line).strip()
        if line:
            steps.append(line)
    return steps


def _parse_critic(text: str) -> tuple[bool, str, str | None]:
    """Parse the critic's output. Same JSON-first / fallback approach as
    _parse_steps: falls back to treating the reply as feedback text and
    asking for one more round unless it starts with "DONE"."""
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "done" in data:
            followup = data.get("followup") or None
            return bool(data["done"]), str(data.get("feedback", "")), followup
    except (json.JSONDecodeError, TypeError):
        pass

    done = text.upper().startswith("DONE")
    return done, text, (None if done else text)


def _render_results(results: list[tuple[str, str]]) -> str:
    if not results:
        return "(no work done yet)"
    return "\n\n".join(f"Step: {step}\nResult: {result}" for step, result in results)


class OrchestratedAgentRuntime:
    """Same public surface as streaming_agent.AgentRuntime (connect / close /
    tools / tool_names / run_stream) so it's a drop-in alternative — see the
    module docstring for the planner/executor/critic loop it runs instead of
    a single flat tool-calling loop."""

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

    @property
    def mcp(self) -> Any:
        """Passthrough so routers that call tools directly (e.g.
        routers/files.py's upload_and_index_document) keep working unchanged
        against this runtime, same as against streaming_agent.AgentRuntime."""
        return self.base.mcp

    async def run_stream(self, session: ChatSession, question: str) -> AsyncIterator[dict[str, Any]]:
        session.touch()
        full_question = _with_attached_documents_note(session, question)
        session.memory.add_user(full_question)

        yield events.status("planning")
        plan_text = await self._call(session, _PLANNER_INSTRUCTIONS.format(question=full_question))
        steps = _parse_steps(plan_text)[:_MAX_PLAN_STEPS] or [full_question]
        yield events.plan(steps)

        results: list[tuple[str, str]] = []
        for i, step in enumerate(steps, 1):
            yield events.handoff("executor", step)
            yield events.status(f"executing step {i}/{len(steps)}")
            async for kind, payload in self._run_executor(session, step):
                if kind == "event":
                    yield payload
                else:
                    results.append((step, payload))

        for round_num in range(1, _MAX_CRITIC_ROUNDS + 1):
            yield events.status(f"critic review {round_num}/{_MAX_CRITIC_ROUNDS}")
            critic_text = await self._call(
                session, _CRITIC_INSTRUCTIONS.format(question=full_question, work=_render_results(results))
            )
            done, feedback, followup = _parse_critic(critic_text)
            yield events.critic(done, feedback, followup)
            if done or not followup:
                break
            yield events.handoff("executor", followup)
            async for kind, payload in self._run_executor(session, followup):
                if kind == "event":
                    yield payload
                else:
                    results.append((followup, payload))

        yield events.status("finalizing")
        final = await self._call(
            session, _FINAL_INSTRUCTIONS.format(question=full_question, work=_render_results(results))
        )
        session.memory.add_assistant(final)
        yield events.message(final)
        yield events.done(final, len(results))

    async def _call(self, session: ChatSession, instructions: str) -> str:
        """One-shot, tool-free LLM call for planning/critique/finalization.

        Reuses the session's system prompt and prior visible turns for
        context, but sends `instructions` as an extra scratch message that
        is never persisted into session.memory — the visible conversation
        only ever ends up showing the user's original question and the
        final answer, never the planner/critic scaffolding.
        """
        messages = [*session.memory.messages(), {"role": "user", "content": instructions}]
        response = await self.base.llm.chat.completions.create(
            model=app_settings.chat_model,
            messages=messages,
            temperature=self.base.settings.temperature,
            extra_body=get_ollama_extra_body(),
        )
        return response.choices[0].message.content or ""

    async def _run_executor(self, session: ChatSession, instructions: str) -> AsyncIterator[tuple[str, Any]]:
        """Run one step as its own bounded tool-calling loop, isolated from
        the session's visible history: swaps in a scratch ConversationMemory
        seeded with just this step's instructions, reusing the base
        runtime's _chat/_handle_tool_call so interrupts, ask_user, and
        user-scoped-tool injection (which key off `session`, not
        `session.memory`) all keep working unchanged, then restores the
        session's real memory before returning.

        Yields ("event", sse_dict) for every event the underlying turn
        produces, then exactly one ("result", answer_text) at the end.
        """
        real_memory = session.memory
        scratch = ConversationMemory(
            _EXECUTOR_SYSTEM_PROMPT,
            real_memory.max_messages,
            real_memory.max_tool_result_chars,
            real_memory.max_context_tokens,
        )
        scratch.add_user(instructions)
        session.memory = scratch
        try:
            answer = ""
            for _ in range(_EXECUTOR_ITERATION_BUDGET):
                response = None
                async for kind, payload in self.base._chat(session, tools_enabled=True):
                    if kind == "event":
                        yield ("event", payload)
                    elif kind == "response":
                        response = payload
                    elif kind == "failed":
                        yield ("result", answer or "(executor failed before producing a result)")
                        return
                if response is None:
                    yield ("result", answer or "(executor failed before producing a result)")
                    return

                message = response.choices[0].message
                reasoning = _extract_reasoning(message)
                if reasoning:
                    yield ("event", events.reasoning(reasoning))

                if not message.tool_calls:
                    answer = message.content or ""
                    scratch.add_assistant(answer)
                    break

                scratch.add_assistant(message.content, tool_calls=[tc.model_dump() for tc in message.tool_calls])
                for call in message.tool_calls:
                    async for event in self.base._handle_tool_call(session, call):
                        yield ("event", event)
            yield ("result", answer or "(executor made no progress on this step)")
        finally:
            session.memory = real_memory
