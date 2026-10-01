"""Multi-user access-control rules in the chat API: which tools the model can
see, which tool calls need approval, document ownership on tool calls, and
owner checks on the session/interrupt/question routes.

No MCP servers, LLM, or MongoDB needed — the runtime, registry and stores
are replaced with small in-memory fakes.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.apps.api import main as api_main
from backend.apps.api.auth import AuthUser, get_current_user
from backend.apps.api.sessions import ChatSession
from backend.apps.api.streaming_agent import AgentRuntime
from backend.apps.client.memory import ConversationMemory
from backend.config import settings as app_settings


# --- fakes ---------------------------------------------------------------


class FakeRegistry:
    def __init__(self, owners: dict[str, str]) -> None:
        self.owners = owners

    async def is_owner(self, document_id: str, user_id: str) -> bool:
        return self.owners.get(document_id) == user_id


class FakeMcp:
    def __init__(self, tools: list[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._tools = tools or []

    async def list_tools(self) -> list[Any]:
        return [SimpleNamespace(name=n, description="", inputSchema={"type": "object", "properties": {}}) for n in self._tools]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, dict(arguments)))
        return json.dumps({"success": True})


def _runtime(tools: list[dict[str, Any]], registry: FakeRegistry | None) -> AgentRuntime:
    runtime = AgentRuntime(FakeMcp(), llm_client=None, settings=app_settings.api, isolated_sessions=False)  # type: ignore[arg-type]
    runtime.register_tool_schemas(
        [(t["function"]["name"], t["function"]["description"], t["function"]["parameters"]) for t in tools]
    )
    runtime.document_registry = registry
    return runtime


def _tool(name: str, *params: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": "", "parameters": {"type": "object", "properties": {p: {"type": "string"} for p in params}}},
    }


def _session(user_id: str, mcp: FakeMcp, approval: list[str] | None = None) -> ChatSession:
    return ChatSession(
        session_id="s1",
        memory=ConversationMemory("sys", 40, 8000, None),
        approval_required_tools=approval or [],
        mcp=mcp,  # type: ignore[arg-type]
        mcp_owns_lifecycle=False,
        user_id=user_id,
    )


def _call(name: str, arguments: str) -> Any:
    return SimpleNamespace(id="call_1", function=SimpleNamespace(name=name, arguments=arguments))


async def _drain(gen: Any) -> list[dict[str, Any]]:
    return [event async for event in gen]


def _results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [json.loads(e["data"]) for e in events if e["event"] == "tool_result"]


# --- model tool allowlist -------------------------------------------------


def test_model_only_sees_allowlisted_tools() -> None:
    mcp = FakeMcp(["retrieval_retrieve_document_v2", "indexing_upload_document", "retrieval_get_document_chunks_v2"])
    runtime = AgentRuntime(mcp, llm_client=None, settings=app_settings.api, isolated_sessions=False)  # type: ignore[arg-type]
    asyncio.run(runtime._load_tools())
    assert "retrieval_retrieve_document_v2" in runtime.tool_names
    assert "indexing_upload_document" not in runtime.tool_names
    assert "retrieval_get_document_chunks_v2" not in runtime.tool_names


def test_hidden_tool_call_is_refused_without_dispatch() -> None:
    mcp = FakeMcp()
    runtime = _runtime([_tool("retrieval_retrieve_document_v2", "document_id")], FakeRegistry({}))
    session = _session("alice", mcp)
    events = asyncio.run(_drain(runtime._handle_tool_call(session, _call("indexing_upload_document", '{"file_path": "/proc/self/environ"}'), [], {})))
    assert _results(events)[0]["ok"] is False
    assert mcp.calls == []


# --- approval policy --------------------------------------------------------


def test_mandatory_approval_cannot_be_opted_out() -> None:
    session = _session("alice", FakeMcp(), approval=[])
    assert session.requires_approval("db_insert_data")
    assert not session.requires_approval("retrieval_retrieve_document_v2")


def test_session_can_add_approvals() -> None:
    session = _session("alice", FakeMcp(), approval=["code_run_python_code"])
    assert session.requires_approval("code_run_python_code")


# --- document ownership on tool calls ---------------------------------------


def test_tool_call_on_someone_elses_document_is_denied() -> None:
    mcp = FakeMcp()
    runtime = _runtime([_tool("code_profile_spreadsheet", "document_id")], FakeRegistry({"doc-b": "bob"}))
    session = _session("alice", mcp)
    events = asyncio.run(_drain(runtime._handle_tool_call(session, _call("code_profile_spreadsheet", '{"document_id": "doc-b"}'), [], {})))
    assert _results(events)[0]["ok"] is False
    assert mcp.calls == []


def test_tool_call_on_own_document_runs_with_injected_user_id() -> None:
    mcp = FakeMcp()
    runtime = _runtime([_tool("retrieval_retrieve_document_v2", "document_id", "query", "user_id")], FakeRegistry({"doc-a": "alice"}))
    session = _session("alice", mcp)
    args = '{"document_id": "doc-a", "query": "q", "user_id": "bob"}'
    events = asyncio.run(_drain(runtime._handle_tool_call(session, _call("retrieval_retrieve_document_v2", args), [], {})))
    assert _results(events)[0]["ok"] is True
    assert mcp.calls == [("retrieval_retrieve_document_v2", {"document_id": "doc-a", "query": "q", "user_id": "alice"})]


def test_document_ids_list_is_checked_too() -> None:
    mcp = FakeMcp()
    runtime = _runtime([_tool("some_tool", "document_ids")], FakeRegistry({"doc-a": "alice", "doc-b": "bob"}))
    session = _session("alice", mcp)
    events = asyncio.run(_drain(runtime._handle_tool_call(session, _call("some_tool", '{"document_ids": ["doc-a", "doc-b"]}'), [], {})))
    assert _results(events)[0]["ok"] is False
    assert mcp.calls == []


def test_malformed_arguments_become_an_error_result() -> None:
    mcp = FakeMcp()
    runtime = _runtime([_tool("retrieval_web_search", "query")], FakeRegistry({}))
    session = _session("alice", mcp)
    events = asyncio.run(_drain(runtime._handle_tool_call(session, _call("retrieval_web_search", "{not json"), [], {})))
    assert _results(events)[0]["ok"] is False
    assert mcp.calls == []


# --- route-level owner checks ----------------------------------------------


class FakeConversationStore:
    def __init__(self, owners: dict[str, str]) -> None:
        self.owners = owners
        self.deleted: list[str] = []

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        owner = self.owners.get(conversation_id)
        return None if owner is None else {"_id": conversation_id, "user_id": owner}

    async def delete_conversation(self, conversation_id: str) -> None:
        self.deleted.append(conversation_id)


class FakeSessionManager:
    def __init__(self, sessions: dict[str, ChatSession]) -> None:
        self.sessions = sessions

    def get(self, session_id: str) -> ChatSession | None:
        return self.sessions.get(session_id)

    async def delete(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)


@pytest.fixture
def client_as():
    def make(user_id: str, sessions: dict[str, ChatSession], store: FakeConversationStore) -> TestClient:
        app = api_main.create_app()
        app.state.session_manager = FakeSessionManager(sessions)
        app.state.conversation_store = store
        app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id=user_id, email=f"{user_id}@x")
        return TestClient(app)  # no `with`: lifespan (MCP/Mongo connect) is skipped

    return make


def test_cannot_delete_someone_elses_evicted_conversation(client_as) -> None:
    store = FakeConversationStore({"conv-b": "bob"})
    client = client_as("alice", {}, store)
    assert client.delete("/api/sessions/conv-b").status_code == 404
    assert store.deleted == []


def test_can_delete_own_evicted_conversation(client_as) -> None:
    store = FakeConversationStore({"conv-a": "alice"})
    client = client_as("alice", {}, store)
    assert client.delete("/api/sessions/conv-a").status_code == 204
    assert store.deleted == ["conv-a"]


def test_cannot_resolve_someone_elses_interrupt(client_as) -> None:
    async def make_pending() -> tuple[ChatSession, str]:
        session = _session("bob", FakeMcp())
        pending = session.new_interrupt("c1", "db_insert_data", {})
        return session, pending.interrupt_id

    async def scenario() -> tuple[int, bool]:
        session, interrupt_id = await make_pending()
        client = client_as("alice", {"s1": session}, FakeConversationStore({}))
        resp = client.post(f"/api/sessions/s1/interrupts/{interrupt_id}/resume", json={"action": "approve"})
        return resp.status_code, session.pending_interrupts[interrupt_id].future.done()

    status, resolved = asyncio.run(scenario())
    assert status == 404
    assert not resolved


def test_cannot_answer_someone_elses_question(client_as) -> None:
    async def scenario() -> int:
        session = _session("bob", FakeMcp())
        pending = session.new_question("c1", "which?", [], allow_other=True)
        client = client_as("alice", {"s1": session}, FakeConversationStore({}))
        return client.post(f"/api/sessions/s1/questions/{pending.question_id}/answer", json={"answer": "x"}).status_code

    assert asyncio.run(scenario()) == 404
