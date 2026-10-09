"""SSE event payload builders.

Every event sent to the frontend is a dict shaped as:
    {"event": <type>, "data": <json-string>}
which `sse_starlette.EventSourceResponse` writes verbatim as an SSE frame.

Event types
-----------
session   - session_id + connected tool names (sent once, stream open)
status    - free-text progress note ("thinking", "iteration 2/15", ...)
reasoning - the model's chain-of-thought for one LLM call, when the model
            returns one (currently: gpt-oss:20b via Ollama's "reasoning" field)
tool_call - the model requested a tool call (call_id, name, arguments)
interrupt - a tool call is paused pending human approval (interrupt_id, ...)
question  - the model is asking the human a question and needs an answer
            before it can continue (question_id, prompt, options, allow_other)
tool_result - a tool call finished (call_id, name, ok, result)
citations - the distinct source chunks retrieved this turn (deduped by
            chunk_id, numbered in first-appearance order), sent once right
            before "message" so the frontend can resolve any [n] markers
            the model wrote in its answer against this list
message   - the assistant's final natural-language answer for this turn
error     - something went wrong; the stream ends after this
done      - turn finished (echoes the answer + iteration count)

"""
from __future__ import annotations

import json
from typing import Any


def sse(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


def status(message: str, **extra: Any) -> dict[str, Any]:
    return sse("status", {"message": message, **extra})


def reasoning(content: str) -> dict[str, Any]:
    return sse("reasoning", {"content": content})


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return sse("tool_call", {"call_id": call_id, "name": name, "arguments": arguments})


def interrupt(interrupt_id: str, call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return sse(
        "interrupt",
        {"interrupt_id": interrupt_id, "call_id": call_id, "name": name, "arguments": arguments},
    )


def question(
    question_id: str,
    call_id: str,
    prompt: str,
    options: list[str] | None,
    allow_other: bool,
) -> dict[str, Any]:
    return sse(
        "question",
        {
            "question_id": question_id,
            "call_id": call_id,
            "prompt": prompt,
            "options": options or [],
            "allow_other": allow_other,
        },
    )


def tool_result(call_id: str, name: str, ok: bool, result: str) -> dict[str, Any]:
    return sse("tool_result", {"call_id": call_id, "name": name, "ok": ok, "result": result})


def citations(items: list[dict[str, Any]]) -> dict[str, Any]:
    return sse("citations", {"items": items})


def token(content: str) -> dict[str, Any]:
    """A piece of the model's reply as it is generated (display only; the
    final `message` event carries the full, citation-rendered answer)."""
    return sse("token", {"content": content})


def message(content: str) -> dict[str, Any]:
    return sse("message", {"content": content})


def error(detail: str) -> dict[str, Any]:
    return sse("error", {"detail": detail})


def usage(summary: dict[str, Any]) -> dict[str, Any]:
    """What this turn used: model calls, tool calls, tokens, cost, duration."""
    return sse("usage", summary)


def done(answer: str, iterations: int) -> dict[str, Any]:
    return sse("done", {"answer": answer, "iterations": iterations})
