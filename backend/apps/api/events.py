"""SSE event payload builders.

Every event sent to the frontend is a dict shaped as:
    {"event": <type>, "data": <json-string>}
which `sse_starlette.EventSourceResponse` writes verbatim as an SSE frame.

Event types
-----------
session   - session_id + connected tool names (sent once, stream open)
status    - free-text progress note ("thinking", "iteration 2/15", ...)
tool_call - the model requested a tool call (call_id, name, arguments)
interrupt - a tool call is paused pending human approval (interrupt_id, ...)
tool_result - a tool call finished (call_id, name, ok, result)
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


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return sse("tool_call", {"call_id": call_id, "name": name, "arguments": arguments})


def interrupt(interrupt_id: str, call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return sse(
        "interrupt",
        {"interrupt_id": interrupt_id, "call_id": call_id, "name": name, "arguments": arguments},
    )


def tool_result(call_id: str, name: str, ok: bool, result: str) -> dict[str, Any]:
    return sse("tool_result", {"call_id": call_id, "name": name, "ok": ok, "result": result})


def message(content: str) -> dict[str, Any]:
    return sse("message", {"content": content})


def error(detail: str) -> dict[str, Any]:
    return sse("error", {"detail": detail})


def done(answer: str, iterations: int) -> dict[str, Any]:
    return sse("done", {"answer": answer, "iterations": iterations})
