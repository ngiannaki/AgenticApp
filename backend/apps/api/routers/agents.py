"""Agents and their tasks, for the signed-in user. Generic over agents:
whatever the agents service registers is listed and runnable here."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from backend.apps.api.agent_tasks import TERMINAL, AgentHub
from backend.apps.api.auth import AuthUser, get_current_user

router = APIRouter(prefix="/api", tags=["agents"])


def _hub(request: Request) -> AgentHub:
    hub = getattr(request.app.state, "agent_hub", None)
    if hub is None:
        raise HTTPException(status_code=503, detail="Agents are not enabled on this server (AGENTS_INTERNAL_TOKEN).")
    return hub


class StartTask(BaseModel):
    agent_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class Reply(BaseModel):
    text: str = Field(min_length=1)


class UpdateTask(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class Rerun(BaseModel):
    feedback: str = ""
    inputs: dict[str, Any] | None = None
    """Edited inputs; omit to reuse the original task's."""


@router.get("/agents")
async def list_agents(request: Request, user: AuthUser = Depends(get_current_user)) -> list[dict]:
    try:
        return await _hub(request).agents()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="The agents service is not reachable.")


async def _check_documents(request: Request, inputs: dict, user: AuthUser) -> None:
    """Documents named in the inputs must be the user's own."""
    registry = request.app.state.document_registry
    for value in inputs.values():
        for doc_id in value if isinstance(value, list) else []:
            if not await registry.is_owner(str(doc_id), user.user_id):
                raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")


@router.post("/tasks", status_code=201)
async def start_task(body: StartTask, request: Request, user: AuthUser = Depends(get_current_user)) -> dict:
    hub = _hub(request)
    await _check_documents(request, body.inputs, user)
    try:
        return await hub.start(user.user_id, body.agent_id, body.inputs)
    except LookupError:
        raise HTTPException(status_code=404, detail="Unknown agent")
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc))


@router.get("/tasks")
async def list_tasks(request: Request, agent_id: str | None = None, user: AuthUser = Depends(get_current_user)) -> list[dict]:
    return await _hub(request).store.list(user.user_id, agent_id)


async def _own_task(request: Request, task_id: str, user: AuthUser) -> dict:
    task = await _hub(request).store.get(task_id, user.user_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request, user: AuthUser = Depends(get_current_user)) -> dict:
    task = await _own_task(request, task_id, user)
    task["events"] = await _hub(request).store.events(task_id)
    return task


@router.post("/tasks/{task_id}/reply", status_code=202)
async def reply(task_id: str, body: Reply, request: Request, user: AuthUser = Depends(get_current_user)) -> dict:
    task = await _own_task(request, task_id, user)
    if task["status"] != "input_required":
        raise HTTPException(status_code=409, detail="This task isn't waiting for an answer")
    await _hub(request).reply(task, body.text)
    return {"ok": True}


@router.post("/tasks/{task_id}/cancel", status_code=202)
async def cancel(task_id: str, request: Request, user: AuthUser = Depends(get_current_user)) -> dict:
    task = await _own_task(request, task_id, user)
    if task["status"] not in TERMINAL:
        await _hub(request).cancel(task)
    return {"ok": True}


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, body: UpdateTask, request: Request, user: AuthUser = Depends(get_current_user)) -> dict:
    await _own_task(request, task_id, user)
    await _hub(request).store.update(task_id, title=body.title.strip())
    return await _own_task(request, task_id, user)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: str, request: Request, user: AuthUser = Depends(get_current_user)) -> None:
    task = await _own_task(request, task_id, user)
    await _hub(request).delete(task)


@router.post("/tasks/{task_id}/rerun", status_code=201)
async def rerun_task(task_id: str, body: Rerun, request: Request, user: AuthUser = Depends(get_current_user)) -> dict:
    """A new task for the same agent, with edited inputs and/or feedback on
    the previous result; linked to the original via parent_task_id."""
    task = await _own_task(request, task_id, user)
    inputs = body.inputs if body.inputs is not None else task["inputs"]
    await _check_documents(request, inputs, user)
    try:
        return await _hub(request).start(user.user_id, task["agent_id"], inputs, parent=task, feedback=body.feedback.strip())
    except LookupError:
        raise HTTPException(status_code=404, detail="That agent is no longer available")
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc))


@router.get("/tasks/{task_id}/events")
async def stream_events(task_id: str, request: Request, after: int = 0, user: AuthUser = Depends(get_current_user)) -> EventSourceResponse:
    """Replays the task's events after `after`, then streams new ones until the task ends."""
    await _own_task(request, task_id, user)
    store = _hub(request).store

    async def gen():
        last = after
        while True:
            for event in await store.events(task_id, last):
                last = event["seq"]
                yield {"event": "task_event", "data": json.dumps(event, default=str)}
            task = await store.get(task_id)
            if task is None or task["status"] in TERMINAL:
                for event in await store.events(task_id, last):
                    yield {"event": "task_event", "data": json.dumps(event, default=str)}
                yield {"event": "end", "data": json.dumps({"status": task["status"] if task else "gone"})}
                return
            await asyncio.sleep(1.0)

    return EventSourceResponse(gen(), ping=15)
