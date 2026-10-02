"""User-built agents and skills (app.custom_agents, app.agent_skills).

A skill is reusable instructions plus the capabilities it may use (tool
groups from backend/core/agent_tools.py: documents, web, database, code).
An agent is a name, instructions and a list of skills. Users create, update
and delete both on the Agents page; tasks run on the agents service's
generic "custom" runner (backend/apps/agents/custom.py), which gets the
resolved definition from the API — so a definition always reflects the
owner's current agent and every tool call is still checked by the API.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, field_validator

from backend.apps.api.auth import AuthUser, get_current_user
from backend.config import settings as app_settings
from backend.core.agent_tools import TOOL_GROUPS, tools_for
from backend.core.db import run_sync

PREFIX = "custom:"
MAX_PER_USER = 50
_COLS = "id, name, description, instructions, extract(epoch FROM created_at)::float8 AS created_at, extract(epoch FROM updated_at)::float8 AS updated_at"


class CustomAgentStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def _list(self, table: str, extra: str, user_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            return conn.execute(
                f"SELECT {_COLS}, {extra} FROM app.{table} WHERE user_id = %s ORDER BY lower(name)", (user_id,)
            ).fetchall()

    async def skills(self, user_id: str) -> list[dict]:
        return await run_sync(self._list, "agent_skills", "tools", user_id)

    async def agents(self, user_id: str) -> list[dict]:
        return await run_sync(self._list, "custom_agents", "skill_ids", user_id)

    async def save(self, table: str, user_id: str, item_id: str | None, fields: dict[str, Any]) -> dict:
        def _do() -> dict | None:
            with self.pool.connection() as conn:
                values = {k: json.dumps(v) if isinstance(v, list) else v for k, v in fields.items()}
                if item_id is None:
                    count = conn.execute(f"SELECT count(*) AS n FROM app.{table} WHERE user_id = %s", (user_id,)).fetchone()["n"]
                    if count >= MAX_PER_USER:
                        raise PermissionError(f"You can have at most {MAX_PER_USER}.")
                    new_id = uuid.uuid4().hex[:12]
                    cols = ", ".join(values)
                    conn.execute(
                        f"INSERT INTO app.{table} (id, user_id, {cols}) VALUES (%s, %s, {', '.join(['%s'] * len(values))})",
                        (new_id, user_id, *values.values()),
                    )
                    return {"id": new_id}
                sets = ", ".join(f"{k} = %s" for k in values)
                row = conn.execute(
                    f"UPDATE app.{table} SET {sets}, updated_at = now() WHERE id = %s AND user_id = %s RETURNING id",
                    (*values.values(), item_id, user_id),
                ).fetchone()
                return row

        try:
            row = await run_sync(_do)
        except psycopg.errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="You already have one with that name.")
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        return row

    async def delete(self, table: str, user_id: str, item_id: str) -> bool:
        def _do() -> bool:
            with self.pool.connection() as conn:
                deleted = conn.execute(f"DELETE FROM app.{table} WHERE id = %s AND user_id = %s", (item_id, user_id)).rowcount
                if deleted and table == "agent_skills":
                    # Agents keep working without the skill.
                    conn.execute(
                        "UPDATE app.custom_agents SET skill_ids = skill_ids - %s, updated_at = now()"
                        " WHERE user_id = %s AND skill_ids ? %s",
                        (item_id, user_id, item_id),
                    )
                return bool(deleted)

        return await run_sync(_do)

    async def definition(self, user_id: str, agent_id: str) -> tuple[dict, dict] | None:
        """(agent info for the UI/hub, definition for the runner), or None."""
        agent = next((a for a in await self.agents(user_id) if a["id"] == agent_id), None)
        if agent is None:
            return None
        by_id = {s["id"]: s for s in await self.skills(user_id)}
        skills = [by_id[i] for i in agent["skill_ids"] if i in by_id]
        groups = sorted({g for s in skills for g in s["tools"]})
        definition = {
            "name": agent["name"],
            "instructions": agent["instructions"],
            "skills": [{"name": s["name"], "instructions": s["instructions"]} for s in skills],
            "tools": [t for t in tools_for(groups) if _model_tool(t)],
        }
        return to_info(agent, skills), definition


def _model_tool(name: str) -> bool:
    allowed = app_settings.api.model_tools
    return "*" in allowed or name in allowed


def to_info(agent: dict, skills: list[dict]) -> dict:
    """A custom agent in the same shape as a built-in one (GET /api/agents)."""
    groups = sorted({g for s in skills for g in s["tools"]})
    inputs = [
        {"name": "request", "kind": "textarea", "label": "What should the agent do?", "required": True,
         "default": None, "options": [], "help": "", "file_types": []},
    ]
    if {"documents", "code"} & set(groups):
        inputs.append({"name": "document_ids", "kind": "documents", "label": "Documents (optional)", "required": False,
                       "default": None, "options": [], "help": "", "file_types": []})
    return {
        "id": f"{PREFIX}{agent['id']}",
        "name": agent["name"],
        "description": agent["description"] or "Your custom agent.",
        "inputs": inputs,
        "examples": [],
        "custom": True,
        "custom_id": agent["id"],
        "skills": [s["name"] for s in skills],
        "capabilities": groups,
    }


# --- HTTP -------------------------------------------------------------------------

router = APIRouter(prefix="/api", tags=["custom-agents"])


def _store(request: Request) -> CustomAgentStore:
    return CustomAgentStore(request.app.state.db_pool)


class SkillBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    instructions: str = Field(default="", max_length=8000)
    tools: list[str] = Field(default_factory=list)

    @field_validator("tools")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        unknown = [g for g in v if g not in TOOL_GROUPS]
        if unknown:
            raise ValueError(f"Unknown capability: {', '.join(unknown)}")
        return sorted(set(v))


class AgentBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    instructions: str = Field(default="", max_length=8000)
    skill_ids: list[str] = Field(default_factory=list, max_length=20)


@router.get("/agent-capabilities")
async def capabilities(user: AuthUser = Depends(get_current_user)) -> list[dict]:
    return [
        {"id": key, "label": g["label"], "description": g["description"], "tools": [t for t in g["tools"] if _model_tool(t)]}
        for key, g in TOOL_GROUPS.items()
    ]


@router.get("/skills")
async def list_skills(request: Request, user: AuthUser = Depends(get_current_user)) -> list[dict]:
    return await _store(request).skills(user.user_id)


@router.post("/skills", status_code=201)
async def create_skill(body: SkillBody, request: Request, user: AuthUser = Depends(get_current_user)) -> dict:
    row = await _store(request).save("agent_skills", user.user_id, None, _clean(body.model_dump()))
    return next(s for s in await _store(request).skills(user.user_id) if s["id"] == row["id"])


@router.put("/skills/{skill_id}")
async def update_skill(skill_id: str, body: SkillBody, request: Request, user: AuthUser = Depends(get_current_user)) -> dict:
    await _store(request).save("agent_skills", user.user_id, skill_id, _clean(body.model_dump()))
    return next(s for s in await _store(request).skills(user.user_id) if s["id"] == skill_id)


@router.delete("/skills/{skill_id}", status_code=204)
async def delete_skill(skill_id: str, request: Request, user: AuthUser = Depends(get_current_user)) -> None:
    if not await _store(request).delete("agent_skills", user.user_id, skill_id):
        raise HTTPException(status_code=404, detail="Skill not found")


@router.get("/custom-agents")
async def list_custom_agents(request: Request, user: AuthUser = Depends(get_current_user)) -> list[dict]:
    return await _store(request).agents(user.user_id)


async def _own_skills(request: Request, user: AuthUser, skill_ids: list[str]) -> list[str]:
    owned = {s["id"] for s in await _store(request).skills(user.user_id)}
    missing = [s for s in skill_ids if s not in owned]
    if missing:
        raise HTTPException(status_code=404, detail="Skill not found")
    return list(dict.fromkeys(skill_ids))


@router.post("/custom-agents", status_code=201)
async def create_custom_agent(body: AgentBody, request: Request, user: AuthUser = Depends(get_current_user)) -> dict:
    fields = _clean(body.model_dump())
    fields["skill_ids"] = await _own_skills(request, user, body.skill_ids)
    row = await _store(request).save("custom_agents", user.user_id, None, fields)
    return next(a for a in await _store(request).agents(user.user_id) if a["id"] == row["id"])


@router.put("/custom-agents/{agent_id}")
async def update_custom_agent(agent_id: str, body: AgentBody, request: Request, user: AuthUser = Depends(get_current_user)) -> dict:
    fields = _clean(body.model_dump())
    fields["skill_ids"] = await _own_skills(request, user, body.skill_ids)
    await _store(request).save("custom_agents", user.user_id, agent_id, fields)
    return next(a for a in await _store(request).agents(user.user_id) if a["id"] == agent_id)


@router.delete("/custom-agents/{agent_id}", status_code=204)
async def delete_custom_agent(agent_id: str, request: Request, user: AuthUser = Depends(get_current_user)) -> None:
    """Past tasks of the agent stay (they keep their results)."""
    if not await _store(request).delete("custom_agents", user.user_id, agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")


def _clean(fields: dict) -> dict:
    return {k: v.strip() if isinstance(v, str) else v for k, v in fields.items()}
