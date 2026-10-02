"""Agents end to end: the real agents service (A2A over HTTP) driven by the
API's AgentHub, with task events in Postgres. The LLM and tools are scripted
fakes. Needs TEST_DATABASE_ADMIN_URL (see conftest.py)."""
from __future__ import annotations

import asyncio
import json
import socket

import uvicorn

from backend.apps.agents import base as agents_base
from backend.apps.agents import main as agents_main
from backend.apps.agents.base import BaseAgent, Field, ToolResult
from backend.apps.agents.research import DeepResearchAgent
from backend.apps.api.agent_tasks import AgentHub, TaskStore
from backend.config import settings

TOKEN = "test-internal-token"


class EchoAgent(BaseAgent):
    id = "echo"
    name = "Echo"
    description = "test agent"
    inputs = [Field("question", "textarea", "Question", required=True)]

    async def run(self, ctx, inputs):
        await ctx.step("plan", "Thinking", "about it")
        answer = await ctx.ask("Which colour?", ["red", "blue"])
        await ctx.artifact("report.md", text=f"You asked {inputs['question']} and chose {answer}")
        await ctx.artifact("data", data={"n": 1}, kind="json")


class FakeLlm:
    def __init__(self, replies):
        self.replies = list(replies)

    async def json(self, system, user, retries=1):
        return self.replies.pop(0)

    async def text(self, system, user):
        return self.replies.pop(0)


class FakeTools:
    def __init__(self, *a, **k):
        pass

    async def call(self, name, arguments, approved=False):
        if name == "retrieval_search_my_documents_v2":
            return ToolResult(True, json.dumps({"success": True, "source_chunks": [
                {"text": "Revenue grew 12% in 2025.", "grounding": {"chunk_id": "c-1", "document_name": "annual.pdf", "location": 4}}]}))
        return ToolResult(False, "unavailable")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait(store, task_id, statuses, timeout=15):
    for _ in range(int(timeout * 10)):
        task = await store.get(task_id)
        if task["status"] in statuses:
            return task
        await asyncio.sleep(0.1)
    raise AssertionError(f"task stuck in {task['status']}")


def test_agent_task_lifecycle(pg_pool, make_user, monkeypatch) -> None:
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(settings.agents, "internal_token", TOKEN)
    research_llm = FakeLlm([
        {"clarify": None, "sub_questions": ["revenue growth"]},
        'Revenue grew 12% <citation id="c-1"/> and <citation id="made-up"/>.',
    ])
    monkeypatch.setattr(agents_base, "ToolClient", FakeTools)
    monkeypatch.setattr(agents_base, "Llm", lambda: research_llm)
    user = make_user()

    async def scenario():
        app = agents_main.create_app([EchoAgent(), DeepResearchAgent()], base_url=url)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        store = TaskStore(pg_pool)
        hub = AgentHub(store, url, TOKEN)
        try:
            assert [a["id"] for a in await hub.agents()] == ["echo", "deep_research"]

            # Steps, a question, the reply, artifacts, completion.
            task = await hub.start(user, "echo", {"question": "hi"})
            task = await _wait(store, task["id"], {"input_required"})
            await hub.reply(await store.get(task["id"]), "blue")
            await _wait(store, task["id"], {"completed"})
            events = await store.events(task["id"])
            kinds = [e["kind"] for e in events]
            assert "step" in kinds and "question" in kinds and "answer" in kinds
            question = next(e["data"] for e in events if e["kind"] == "question")
            assert question == {"question": "Which colour?", "options": ["red", "blue"]}
            artifacts = {e["data"]["name"]: e["data"] for e in events if e["kind"] == "artifact"}
            assert artifacts["report.md"]["text"] == "You asked hi and chose blue"
            assert artifacts["data"]["data"] == {"n": 1}

            # Deep Research with scripted LLM + tools: citations resolve, invented ones vanish.
            task = await hub.start(user, "deep_research", {"question": "How did revenue grow?", "use_web": False, "depth": "quick"})
            await _wait(store, task["id"], {"completed", "failed"})
            events = await store.events(task["id"])
            report = next(e["data"]["text"] for e in events if e["kind"] == "artifact" and e["data"]["name"] == "report.md")
            assert "Revenue grew 12% [1]" in report and "made-up" not in report and "annual.pdf (p. 4)" in report

            # Rerun with feedback: new linked task; the feedback and previous result reach the agent.
            echo_task = next(t for t in await store.list(user) if t["agent_id"] == "echo")
            rerun = await hub.start(user, "echo", echo_task["inputs"], parent=echo_task, feedback="shorter please")
            assert rerun["parent_task_id"] == echo_task["id"] and rerun["title"].endswith("(rerun)")
            await _wait(store, rerun["id"], {"input_required"})
            rerun_row = await store.get(rerun["id"])
            await hub.delete(rerun_row)
            assert await store.get(rerun["id"]) is None

            # Inputs are validated by the agent: a missing required field fails the task.
            bad = await hub.start(user, "echo", {})
            bad = await _wait(store, bad["id"], {"failed"})
            assert "required" in (bad["error"] or "")
        finally:
            await hub.close()
            server.should_exit = True
            await serving

    asyncio.run(scenario())


def test_extractor_returns_json_with_sources(monkeypatch) -> None:
    from backend.apps.agents.base import RunContext
    from backend.apps.agents.extractor import ExtractorAgent

    llm = FakeLlm([
        {"fields": [{"key": "revenue_growth", "description": "growth %", "query": "revenue growth"},
                    {"key": "ceo", "description": "CEO name", "query": "chief executive"}]},
        '```json\n{"revenue_growth": {"value": 12, "source": "E1", "quote": "Revenue grew  12%"},'
        ' "ceo": {"value": null, "source": null}}\n```',
    ])
    agent = ExtractorAgent()
    ctx = RunContext(agent, "t1", "alice", tools=FakeTools(), llm=llm)
    out: dict = {}

    async def step(*a, **k):
        pass

    async def artifact(name, text=None, data=None, kind="markdown"):
        out.update(name=name, data=data, kind=kind)

    ctx.step, ctx.artifact = step, artifact
    asyncio.run(agent.run(ctx, agent.validate({"request": "summarize growth and CEO"})))
    assert out["kind"] == "json" and out["name"] == "extraction.json"
    assert out["data"]["values"] == {"revenue_growth": 12, "ceo": None}
    src = out["data"]["sources"]["revenue_growth"]
    assert src["document"] == "annual.pdf" and src["location"] == 4 and src["n"] == 1
    assert src["quote"] == "Revenue grew  12%" and src["quote_verified"] is True
    assert out["data"]["citations"] == [{"n": 1, "document": "annual.pdf", "location": 4, "section": None,
                                         "document_id": None, "chunk_id": "c-1", "pages": None}]
    assert out["data"]["sources"]["ceo"] is None


def test_agents_service_requires_the_internal_token(monkeypatch) -> None:
    from starlette.testclient import TestClient

    monkeypatch.setattr(settings.agents, "internal_token", TOKEN)
    client = TestClient(agents_main.create_app([EchoAgent()], base_url="http://x"))
    assert client.get("/agents").status_code == 401
    assert client.get("/agents", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/agents", headers={"Authorization": f"Bearer {TOKEN}"}).json()[0]["id"] == "echo"


def test_execute_tool_enforces_approval_and_ownership() -> None:
    from backend.tests.test_api_access_control import FakeMcp, FakeRegistry, _runtime, _tool

    runtime = _runtime([_tool("db_insert_data", "table"), _tool("code_profile_spreadsheet", "document_id", "file_token")],
                       FakeRegistry({"doc-a": "alice"}))
    runtime.mcp = FakeMcp()
    run = asyncio.run
    assert run(runtime.execute_tool("alice", "db_insert_data", {"table": "t"}))[0] is False  # needs approval
    assert run(runtime.execute_tool("alice", "db_insert_data", {"table": "t"}, approved=True))[0] is True
    assert run(runtime.execute_tool("bob", "code_profile_spreadsheet", {"document_id": "doc-a"}))[0] is False
    ok, _ = run(runtime.execute_tool("alice", "code_profile_spreadsheet", {"document_id": "doc-a"}))
    assert ok and runtime.mcp.calls[-1][1]["file_token"]
    assert run(runtime.execute_tool("alice", "indexing_upload_document", {}))[0] is False  # not exposed
