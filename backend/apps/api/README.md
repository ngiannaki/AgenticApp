# Agentic App API

FastAPI SSE backend that fronts the generic MCP agent (same tool-calling
loop as `backend/apps/client/example_mcp_client.py`) for the Next.js
frontend. Connects once to every MCP server in `servers.json`
(indexing, retrieval, db) at startup and shares that connection across all
chat sessions.

## Run

```bash
# MCP servers must already be running (.\start_mcp_servers.ps1)
python -m backend.apps.api.main
# or, with reload:
uvicorn backend.apps.api.main:app --reload --port 8100
```

Config is read from `backend/.env` (prefix `API_`) — see
`backend/apps/api/config.py` for every knob (CORS origins, port, default
approval-required tools, idle session timeout, etc).

## HTTP contract

- `POST /api/sessions` `{system_prompt?, approval_required_tools?}` → `{session_id, tool_names}`
  `approval_required_tools` lists tool names that must pause for a human
  decision before running; pass `["*"]` to gate every tool call, `[]` to
  disable human-in-the-loop for this session. Omit to use the server default
  (`API_APPROVAL_REQUIRED_TOOLS`, empty by default).
- `GET /api/sessions/{id}` → session summary
- `DELETE /api/sessions/{id}` → drop the session
- `GET /api/tools` → every tool discovered across all connected MCP servers
- `POST /api/sessions/{id}/messages` `{message}` → **SSE stream** of the
  agent's turn. One turn at a time per session (concurrent posts get a 409).
- `POST /api/sessions/{id}/interrupts/{interrupt_id}/resume`
  `{action: "approve"|"reject"|"edit", arguments?, reason?}` → resolves a
  pending human-in-the-loop pause; the still-open SSE stream from the
  `/messages` call above resumes and keeps emitting events.
- `POST /api/sessions/{id}/questions/{question_id}/answer` `{answer}` →
  resolves a pending `ask_user` question (button label or free text); the
  still-open SSE stream resumes the same way.

## SSE event types

| event        | payload                                              |
|--------------|-------------------------------------------------------|
| `status`     | `{message}` — progress note                           |
| `tool_call`  | `{call_id, name, arguments}` — model wants to call a tool |
| `interrupt`  | `{interrupt_id, call_id, name, arguments}` — paused, waiting on `.../resume` |
| `question`   | `{question_id, call_id, prompt, options, allow_other}` — model asked the user something via `ask_user`, waiting on `.../questions/{id}/answer` |
| `tool_result`| `{call_id, name, ok, result}`                          |
| `message`    | `{content}` — assistant's final answer for the turn    |
| `error`      | `{detail}` — stream ends after this                    |
| `done`       | `{answer, iterations}` — turn finished                 |

The connection also carries SSE `ping` comments (interval:
`API_INTERRUPT_HEARTBEAT_SECONDS`, default 15s) so it survives long human
approval waits and reverse-proxy idle timeouts.

## Human-in-the-loop flow

1. Client opens `POST /api/sessions/{id}/messages` and starts reading the
   SSE stream.
2. If the model calls a tool listed in the session's
   `approval_required_tools`, the stream emits `tool_call` then `interrupt`
   and suspends — no further events arrive until resolved.
3. Client shows the pending call to a human, then calls
   `POST /api/sessions/{id}/interrupts/{interrupt_id}/resume` with the
   decision:
   - `approve` — run the tool call as proposed
   - `edit` — run it with `arguments` substituted in
   - `reject` — skip execution; the model sees a rejection message
     (plus optional `reason`) as the tool result and continues
4. The original SSE stream (still open) resumes and keeps emitting events
   for the rest of the turn.

There's a second, separate pause the model can trigger itself: it always has
access to a local `ask_user` tool (not backed by any MCP server) it can call
with `{prompt, options?}` whenever it's blocked on a decision only the user
can make. Calling it emits `tool_call` then `question` and suspends the turn
exactly like an approval interrupt, resolved via
`POST /api/sessions/{id}/questions/{question_id}/answer {answer}` — `answer`
can be the text of one of `options` (rendered as buttons) or arbitrary free
text, since `allow_other` is always true.

## Notes

- All sessions share one MCP connection and Ollama client, created once at
  startup (`backend/apps/api/streaming_agent.py:AgentRuntime`) — sessions
  only hold conversation memory and pending-interrupt state, so they're
  cheap. Idle sessions are evicted after `API_SESSION_IDLE_TIMEOUT_SECONDS`
  (default 1h).
- Uses `ollama.AsyncClient` (not the sync client `agent.py` uses) so the
  event loop isn't blocked on LLM calls while other sessions are streaming.
