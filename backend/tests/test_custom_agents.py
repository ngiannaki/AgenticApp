"""User-built agents and skills: CRUD + ownership through the API router,
and a run on the real agents service's "custom" runner (scripted LLM)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI
from starlette.testclient import TestClient

from backend.apps.agents import base as agents_base
from backend.apps.agents import main as agents_main
from backend.apps.agents.custom import CustomAgent
from backend.apps.api import custom_agents
from backend.apps.api.agent_tasks import AgentHub, TaskStore
from backend.apps.api.auth import AuthUser, get_current_user
from backend.apps.api.custom_agents import CustomAgentStore
from backend.config import settings
from backend.tests.test_agents import TOKEN, FakeTools, _free_port, _wait


def _client(pg_pool, user_id: str) -> TestClient:
    app = FastAPI()
    app.state.db_pool = pg_pool
    app.include_router(custom_agents.router)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id=user_id, email="x@example.com")
    return TestClient(app)


def test_skills_and_agents_crud(pg_pool, make_user) -> None:
    alice, bob = make_user(), make_user()
    a, b = _client(pg_pool, alice), _client(pg_pool, bob)
    assert {c["id"] for c in a.get("/api/agent-capabilities").json()} == {"documents", "web", "database", "code", "reports", "ui"}

    skill = a.post("/api/skills", json={"name": "Invoices", "instructions": "Read invoices", "tools": ["documents", "code"]}).json()
    assert skill["tools"] == ["code", "documents"]
    assert a.post("/api/skills", json={"name": "Bad", "tools": ["shell"]}).status_code == 422
    assert a.post("/api/skills", json={"name": "Invoices"}).status_code == 409

    agent = a.post("/api/custom-agents", json={"name": "AP clerk", "instructions": "Be precise", "skill_ids": [skill["id"]]}).json()
    assert agent["skill_ids"] == [skill["id"]]
    # Bob can't use, see or change Alice's things.
    assert b.post("/api/custom-agents", json={"name": "Thief", "skill_ids": [skill["id"]]}).status_code == 404
    assert b.get("/api/custom-agents").json() == []
    assert b.put(f"/api/skills/{skill['id']}", json={"name": "Mine"}).status_code == 404
    assert b.delete(f"/api/custom-agents/{agent['id']}").status_code == 404

    updated = a.put(f"/api/skills/{skill['id']}", json={"name": "Invoices", "instructions": "v2", "tools": ["web"]}).json()
    assert updated["instructions"] == "v2" and updated["tools"] == ["web"]
    info, definition = asyncio.run(CustomAgentStore(pg_pool).definition(alice, agent["id"]))
    assert info["id"] == f"custom:{agent['id']}" and info["capabilities"] == ["web"]
    assert definition["tools"] == ["retrieval_fetch_page", "retrieval_web_search"]
    assert definition["skills"] == [{"name": "Invoices", "instructions": "v2"}]

    # Deleting a skill removes it from the agents using it.
    assert a.delete(f"/api/skills/{skill['id']}").status_code == 204
    assert a.get("/api/custom-agents").json()[0]["skill_ids"] == []
    assert a.delete(f"/api/custom-agents/{agent['id']}").status_code == 204


class ScriptedLlm:
    """complete(): first a tool call per script entry, then the final text."""

    def __init__(self, calls, final):
        self.calls, self.final = list(calls), final

    async def complete(self, messages, tools=None):
        if self.calls:
            name, args = self.calls.pop(0)
            call = SimpleNamespace(id=f"c{len(self.calls)}", function=SimpleNamespace(name=name, arguments=json.dumps(args)),
                                   model_dump=lambda: {"id": "c", "type": "function", "function": {"name": name, "arguments": "{}"}})
            return SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(content=self.final, tool_calls=None)


class RecordingTools(FakeTools):
    seen: list = []

    async def call(self, name, arguments, approved=False):
        RecordingTools.seen.append((name, approved))
        if name == "db_insert_data":
            return agents_base.ToolResult(True, '{"success": true}')
        if name == "code_generate_ui":
            return agents_base.ToolResult(True, json.dumps({"success": True, "filename": "dash.html", "content_type": "text/html",
                                                            "url": "http://x/api/files/a1/dash.html", "size_bytes": 10}))
        return await super().call(name, arguments, approved)


def test_custom_agent_runs_with_its_tools_and_asks_before_changes(pg_pool, make_user, monkeypatch) -> None:
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(settings.agents, "internal_token", TOKEN)
    llm = ScriptedLlm(
        [("retrieval_search_my_documents_v2", {"query": "revenue"}), ("code_run_python_code", {"code": "1"}),
         ("db_insert_data", {"table": "t", "records": []}), ("code_generate_ui", {"html": "<b>dash</b>"})],
        "## Answer\nRevenue grew 12% [annual.pdf, p. 4]",
    )
    monkeypatch.setattr(agents_base, "ToolClient", RecordingTools)
    monkeypatch.setattr(agents_base, "Llm", lambda: llm)
    RecordingTools.seen = []

    async def schemas(self, names):
        return [{"type": "function", "function": {"name": n, "parameters": {}}} for n in sorted(names)]

    monkeypatch.setattr(RecordingTools, "schemas", schemas, raising=False)
    user = make_user()
    definition = {"name": "Analyst Ann", "instructions": "Be brief", "skills": [{"name": "S", "instructions": "x"}],
                  "tools": ["retrieval_search_my_documents_v2", "db_insert_data", "code_generate_ui"]}
    info = {"id": "custom:abc", "name": "Analyst Ann", "inputs": CustomAgent().describe()["inputs"]}

    async def scenario():
        app = agents_main.create_app([CustomAgent()], base_url=url)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        store = TaskStore(pg_pool)
        hub = AgentHub(store, url, TOKEN)
        try:
            task = await hub.start(user, "custom:abc", {"request": "How did revenue do?", "document_ids": []}, custom=(info, definition))
            assert task["agent_id"] == "custom:abc" and "_definition" not in task["inputs"]
            await _wait(store, task["id"], {"input_required"})  # db_insert_data needs approval
            question = [e for e in await store.events(task["id"]) if e["kind"] == "question"][-1]["data"]
            assert question["question"].startswith("Allow the agent to run db_insert_data")
            await hub.reply(await store.get(task["id"]), "Approve")
            await _wait(store, task["id"], {"completed"})
            events = await store.events(task["id"])
            report = next(e["data"]["text"] for e in events if e["kind"] == "artifact")
            assert "Revenue grew 12%" in report
            files = next(e["data"]["data"] for e in events if e["kind"] == "artifact" and e["data"].get("kind") == "files")
            assert files == [{"url": "http://x/api/files/a1/dash.html", "filename": "dash.html", "content_type": "text/html", "size_bytes": 10}]
            # Search ran; the code tool wasn't granted so it never reached the API; the insert ran approved.
            assert RecordingTools.seen == [("retrieval_search_my_documents_v2", False), ("db_insert_data", True),
                                           ("code_generate_ui", False)]
        finally:
            await hub.close()
            server.should_exit = True
            await serving

    asyncio.run(scenario())
