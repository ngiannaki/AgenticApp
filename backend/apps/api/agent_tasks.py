"""Agent tasks on the API side: Postgres storage plus the A2A client that
drives the agents service (backend/apps/agents).

Generic over agents: whatever the agents service registers shows up in
GET /api/agents, and the same task lifecycle (start -> steps/questions ->
artifacts -> done) works for all of them. A task's events are written to
Postgres as they stream in, so a task survives the browser closing and its
page can replay the whole history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import new_text_part
from a2a.types import CancelTaskRequest, Message, Role, SendMessageRequest
from google.protobuf.json_format import MessageToDict
from psycopg_pool import ConnectionPool

from backend.core.db import run_sync

logger = logging.getLogger("agentic_api")

TERMINAL = {"completed", "failed", "canceled"}
ACTIVE = {"submitted", "working", "input_required"}
_STATE = {
    "TASK_STATE_SUBMITTED": "submitted",
    "TASK_STATE_WORKING": "working",
    "TASK_STATE_INPUT_REQUIRED": "input_required",
    "TASK_STATE_COMPLETED": "completed",
    "TASK_STATE_FAILED": "failed",
    "TASK_STATE_CANCELED": "canceled",
    "TASK_STATE_REJECTED": "failed",
}
_TASK_COLUMNS = (
    "id, user_id, agent_id, title, status, inputs, error, parent_task_id,"
    " extract(epoch FROM created_at)::float8 AS created_at, extract(epoch FROM updated_at)::float8 AS updated_at"
)


class TaskStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    async def create(self, user_id: str, agent_id: str, title: str, inputs: dict, parent_task_id: str | None = None) -> dict:
        task_id = str(uuid.uuid4())

        def _do():
            with self.pool.connection() as conn:
                return conn.execute(
                    f"INSERT INTO app.agent_tasks (id, user_id, agent_id, title, status, inputs, parent_task_id)"
                    f" VALUES (%s, %s, %s, %s, 'submitted', %s::jsonb, %s) RETURNING {_TASK_COLUMNS}",
                    (task_id, user_id, agent_id, title[:200], json.dumps(inputs), parent_task_id),
                ).fetchone()

        return await run_sync(_do)

    async def get(self, task_id: str, user_id: str | None = None) -> dict | None:
        def _do():
            with self.pool.connection() as conn:
                row = conn.execute(
                    f"SELECT {_TASK_COLUMNS}, a2a_task_id, a2a_context_id FROM app.agent_tasks WHERE id = %s", (task_id,)
                ).fetchone()
            return row if row and (user_id is None or row["user_id"] == user_id) else None

        return await run_sync(_do)

    async def list(self, user_id: str, agent_id: str | None = None) -> list[dict]:
        def _do():
            with self.pool.connection() as conn:
                sql = f"SELECT {_TASK_COLUMNS} FROM app.agent_tasks WHERE user_id = %s"
                params: list = [user_id]
                if agent_id:
                    sql += " AND agent_id = %s"
                    params.append(agent_id)
                return conn.execute(sql + " ORDER BY created_at DESC LIMIT 100", params).fetchall()

        return await run_sync(_do)

    async def count_active(self, user_id: str) -> int:
        def _do():
            with self.pool.connection() as conn:
                return conn.execute(
                    "SELECT count(*) AS n FROM app.agent_tasks WHERE user_id = %s AND status = ANY(%s)",
                    (user_id, list(ACTIVE)),
                ).fetchone()["n"]

        return await run_sync(_do)

    async def update(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = %s" for k in fields)

        def _do():
            with self.pool.connection() as conn:
                conn.execute(f"UPDATE app.agent_tasks SET {cols}, updated_at = now() WHERE id = %s", [*fields.values(), task_id])

        await run_sync(_do)

    async def delete(self, task_id: str) -> None:
        def _do():
            with self.pool.connection() as conn:
                conn.execute("DELETE FROM app.agent_tasks WHERE id = %s", (task_id,))

        await run_sync(_do)

    async def add_event(self, task_id: str, kind: str, data: dict) -> None:
        def _do():
            with self.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO app.agent_task_events (task_id, kind, data) VALUES (%s, %s, %s::jsonb)",
                    (task_id, kind, json.dumps(data, default=str)),
                )

        await run_sync(_do)

    async def events(self, task_id: str, after: int = 0) -> list[dict]:
        def _do():
            with self.pool.connection() as conn:
                return conn.execute(
                    "SELECT seq, kind, data, extract(epoch FROM created_at)::float8 AS at"
                    " FROM app.agent_task_events WHERE task_id = %s AND seq > %s ORDER BY seq",
                    (task_id, after),
                ).fetchall()

        return await run_sync(_do)

    async def fail_interrupted(self) -> int:
        """Tasks left running by an API restart can't be resumed (their stream
        is gone); mark them failed so the UI offers to start again."""

        def _do():
            with self.pool.connection() as conn:
                cur = conn.execute(
                    "UPDATE app.agent_tasks SET status = 'failed', error = 'Interrupted by a server restart.',"
                    " updated_at = now() WHERE status IN ('submitted', 'working')"
                )
                return cur.rowcount

        return await run_sync(_do)


def _parse_artifact(artifact: dict) -> dict:
    meta = artifact.get("metadata") or {}
    texts = [p.get("text", "") for p in artifact.get("parts", []) if "text" in p]
    data = None
    if meta.get("has_data") and texts:
        try:
            data = json.loads(texts.pop())
        except ValueError:
            data = None
    return {"name": artifact.get("name"), "kind": meta.get("kind", "markdown"), "text": "\n".join(texts) or None, "data": data}


def stream_event_to_records(event: dict) -> tuple[dict, list[tuple[str, dict]]]:
    """One A2A stream event -> (task field updates, events to store)."""
    updates: dict[str, Any] = {}
    records: list[tuple[str, dict]] = []
    if "task" in event:
        task = event["task"]
        updates.update(a2a_task_id=task.get("id"), a2a_context_id=task.get("contextId"))
    elif "statusUpdate" in event:
        status = event["statusUpdate"].get("status", {})
        state = _STATE.get(status.get("state", ""), "working")
        updates["status"] = state
        meta = event["statusUpdate"].get("metadata") or {}
        text = " ".join(p.get("text", "") for p in (status.get("message") or {}).get("parts", []) if "text" in p)
        if meta.get("event") == "step":
            data = {"kind": meta.get("kind"), "title": meta.get("title"), "detail": meta.get("detail")}
            if meta.get("data_json"):
                try:
                    data["data"] = json.loads(meta["data_json"])
                except ValueError:
                    pass
            records.append(("step", data))
        elif meta.get("event") == "question":
            try:
                options = json.loads(meta.get("options_json") or "[]")
            except ValueError:
                options = []
            records.append(("question", {"question": meta.get("question") or text, "options": options}))
        elif state == "failed":
            updates["error"] = text or "The agent failed."
            records.append(("error", {"message": updates["error"]}))
        records.append(("status", {"status": state}))
    elif "artifactUpdate" in event:
        records.append(("artifact", _parse_artifact(event["artifactUpdate"].get("artifact", {}))))
    return updates, records


class AgentHub:
    """A2A client side: talks to the agents service on behalf of users."""

    def __init__(self, store: TaskStore, base_url: str, token: str, max_active_per_user: int = 2) -> None:
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.max_active = max_active_per_user
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=None), headers={"Authorization": f"Bearer {token}"}, trust_env=False
        )
        self._agents: list[dict] = []
        self._agents_at = 0.0
        self._running: set[asyncio.Task] = set()

    async def close(self) -> None:
        for task in list(self._running):
            task.cancel()
        await self._http.aclose()

    async def agents(self) -> list[dict]:
        if not self._agents or time.monotonic() - self._agents_at > 60:
            response = await self._http.get(f"{self.base_url}/agents")
            response.raise_for_status()
            self._agents, self._agents_at = response.json(), time.monotonic()
        return self._agents

    async def start(self, user_id: str, agent_id: str, inputs: dict, parent: dict | None = None, feedback: str = "") -> dict:
        agents = {a["id"]: a for a in await self.agents()}
        if agent_id not in agents:
            raise LookupError(agent_id)
        if await self.store.count_active(user_id) >= self.max_active:
            raise PermissionError(f"You already have {self.max_active} agent tasks running; wait for one to finish.")
        first_text = next((v for v in inputs.values() if isinstance(v, str) and v), agents[agent_id]["name"])
        title = str(inputs.get("question") or inputs.get("request") or first_text)
        if parent is not None:
            title = f"{parent['title']} (rerun)"
        task = await self.store.create(user_id, agent_id, title, inputs, parent["id"] if parent else None)
        sent = dict(inputs)
        if parent is not None and feedback:
            sent = await self._with_feedback(agents[agent_id], parent, sent, feedback)
        message = Message(role=Role.ROLE_USER, message_id=str(uuid.uuid4()), parts=[new_text_part(json.dumps(sent))])
        message.metadata.update({"user_id": user_id})
        self._spawn(task["id"], agent_id, message)
        return task

    async def _with_feedback(self, agent: dict, parent: dict, inputs: dict, feedback: str) -> dict:
        """Generic for any agent: fold the user's feedback (and a short
        excerpt of the previous result) into the agent's main text input."""
        main = next((f["name"] for f in agent["inputs"] if f["kind"] in ("textarea", "text")), None)
        if main is None:
            return inputs
        previous = ""
        for event in await self.store.events(parent["id"]):
            if event["kind"] == "artifact":
                data = event["data"]
                previous += (data.get("text") or json.dumps(data.get("data"), default=str) or "")[:2000] + "\n"
        note = f"\n\nFeedback on a previous attempt — take it into account: {feedback}"
        if previous:
            note += f"\n\nPrevious result (excerpt):\n{previous[:4000]}"
        return {**inputs, main: f"{inputs.get(main) or ''}{note}"}

    async def delete(self, task: dict) -> None:
        if task["status"] not in TERMINAL:
            await self.cancel(task)
        await self.store.delete(task["id"])

    async def reply(self, task: dict, text: str) -> None:
        await self.store.add_event(task["id"], "answer", {"text": text})
        await self.store.update(task["id"], status="working")
        message = Message(
            role=Role.ROLE_USER, message_id=str(uuid.uuid4()), parts=[new_text_part(text)],
            task_id=task["a2a_task_id"] or "", context_id=task["a2a_context_id"] or "",
        )
        message.metadata.update({"user_id": task["user_id"]})
        self._spawn(task["id"], task["agent_id"], message)

    async def cancel(self, task: dict) -> None:
        if task.get("a2a_task_id"):
            try:
                client = await self._client(task["agent_id"])
                await client.cancel_task(CancelTaskRequest(id=task["a2a_task_id"]))
            except Exception:  # noqa: BLE001 - mark it canceled either way
                logger.warning("Could not cancel A2A task %s", task["a2a_task_id"], exc_info=True)
        await self.store.update(task["id"], status="canceled")
        await self.store.add_event(task["id"], "status", {"status": "canceled"})

    async def _client(self, agent_id: str):
        return await create_client(f"{self.base_url}/agents/{agent_id}", ClientConfig(httpx_client=self._http, streaming=True))

    def _spawn(self, task_id: str, agent_id: str, message: Message) -> None:
        runner = asyncio.create_task(self._consume(task_id, agent_id, message))
        self._running.add(runner)
        runner.add_done_callback(self._running.discard)

    async def _consume(self, task_id: str, agent_id: str, message: Message) -> None:
        try:
            client = await self._client(agent_id)
            async for event in client.send_message(SendMessageRequest(message=message)):
                updates, records = stream_event_to_records(MessageToDict(event))
                if updates:
                    await self.store.update(task_id, **updates)
                for kind, data in records:
                    await self.store.add_event(task_id, kind, data)
        except Exception as exc:  # noqa: BLE001 - surface to the user, don't crash the API
            logger.exception("Agent task %s failed", task_id)
            task = await self.store.get(task_id)
            if task and task["status"] not in TERMINAL:
                await self.store.update(task_id, status="failed", error=f"Lost contact with the agent: {exc}")
                await self.store.add_event(task_id, "error", {"message": f"Lost contact with the agent: {exc}"})
                await self.store.add_event(task_id, "status", {"status": "failed"})
