"""The API's half of the code-sandbox contract: inline files become stored
artifacts, spreadsheet tool calls get a one-time file token, and the
internal endpoint redeems a token exactly once."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.apps.api import main as api_main
from backend.apps.api.artifacts import FileTokenStore, materialize_artifacts
from backend.apps.api.routers import files as files_router
from backend.core.storage.artifact_store import ArtifactStore
from backend.tests.test_api_access_control import FakeMcp, FakeRegistry, _call, _drain, _results, _session, _tool


def _inline(name: str, data: bytes) -> dict:
    return {"filename": name, "content_type": "text/csv", "size_bytes": len(data), "data_base64": base64.b64encode(data).decode()}


def test_inline_artifacts_become_urls(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    result = {"success": True, "stdout": "ok", "artifacts": [_inline("out.csv", b"a,b\n"), _inline("../../evil.sh", b"x")]}
    text = materialize_artifacts(json.dumps(result), store, "http://localhost:8100")
    data = json.loads(text)
    assert "data_base64" not in text
    first, second = data["artifacts"]
    assert first["url"].startswith("http://localhost:8100/api/files/") and first["url"].endswith("/out.csv")
    artifact_id = first["url"].split("/")[-2]
    assert store.get_path(artifact_id, "out.csv").read_bytes() == b"a,b\n"
    assert second["filename"] == "evil.sh"  # path components stripped

    # Top-level inline file (generate_file / generate_ui shape) and nested error details.
    top = json.loads(materialize_artifacts(json.dumps({"success": True, **_inline("index.html", b"<p>")}), store, ""))
    assert top["url"].startswith("/api/files/") and "data_base64" not in top
    nested = {"success": False, "details": {"artifacts": [_inline("partial.txt", b"p")]}}
    assert "url" in json.loads(materialize_artifacts(json.dumps(nested), store, ""))["details"]["artifacts"][0]

    assert materialize_artifacts("plain text", store, "") == "plain text"


def test_file_tokens_are_single_use_and_expire() -> None:
    tokens = FileTokenStore(ttl_seconds=60)
    token = tokens.issue("doc-1")
    assert tokens.redeem(token) == "doc-1"
    assert tokens.redeem(token) is None
    assert tokens.redeem("made-up") is None

    short = FileTokenStore(ttl_seconds=0.01)
    stale = short.issue("doc-1")
    time.sleep(0.05)
    assert short.redeem(stale) is None


def test_model_schema_hides_injected_params_and_api_fills_them() -> None:
    from backend.tests.test_api_access_control import _runtime

    mcp = FakeMcp()
    runtime = _runtime(
        [_tool("code_profile_spreadsheet", "document_id", "file_token"), _tool("retrieval_retrieve_document_v2", "document_id", "query", "user_id")],
        FakeRegistry({"doc-a": "alice"}),
    )
    visible = {t["function"]["name"]: set(t["function"]["parameters"]["properties"]) for t in runtime.tools}
    assert visible["code_profile_spreadsheet"] == {"document_id"}
    assert visible["retrieval_retrieve_document_v2"] == {"document_id", "query"}

    session = _session("alice", mcp)
    events = asyncio.run(_drain(runtime._handle_tool_call(
        session, _call("code_profile_spreadsheet", '{"document_id": "doc-a", "file_token": "model-made-this-up"}'), [], {}
    )))
    assert _results(events)[0]["ok"] is True
    (name, args), = mcp.calls
    token = args["file_token"]
    assert token != "model-made-this-up"
    assert runtime.file_tokens.redeem(token) == "doc-a"


def test_tool_results_are_materialized_before_the_model_sees_them(tmp_path, monkeypatch) -> None:
    from backend.tests.test_api_access_control import _runtime

    class InlineMcp(FakeMcp):
        async def call_tool(self, name, arguments):
            self.calls.append((name, dict(arguments)))
            return json.dumps({"success": True, "artifacts": [_inline("plot.png", b"\x89PNG")]})

    runtime = _runtime([_tool("code_run_python_code", "code")], FakeRegistry({}))
    runtime._artifact_store = ArtifactStore(tmp_path)
    session = _session("alice", InlineMcp())
    events = asyncio.run(_drain(runtime._handle_tool_call(session, _call("code_run_python_code", '{"code": "x"}'), [], {})))
    result_text = _results(events)[0]["result"]
    assert "data_base64" not in result_text and "/api/files/" in result_text
    assert "data_base64" not in json.dumps(session.memory.messages())


def _internal_client(monkeypatch, tmp_path, client_host: str):
    from backend.core.storage.document_store import DocumentStore

    store = DocumentStore(tmp_path)
    store.create_document(file_bytes=b"a,b\n1,2\n", document_id="11111111-1111-1111-1111-111111111111", filename="data.csv")
    monkeypatch.setattr(files_router, "_document_store", store)
    from backend.apps.api.routers import internal

    monkeypatch.setattr(internal, "_document_store", store)
    app = api_main.create_app()
    app.state.runtime = SimpleNamespace(file_tokens=FileTokenStore())
    return app, TestClient(app, client=(client_host, 50000))


def test_internal_file_endpoint_redeems_once(monkeypatch, tmp_path) -> None:
    app, client = _internal_client(monkeypatch, tmp_path, "172.18.0.5")
    doc_id = "11111111-1111-1111-1111-111111111111"
    token = app.state.runtime.file_tokens.issue(doc_id)

    first = client.get(f"/internal/files/{token}")
    assert first.status_code == 200 and first.content == b"a,b\n1,2\n"
    assert first.headers["x-document-id"] == doc_id and first.headers["x-document-suffix"] == ".csv"
    assert client.get(f"/internal/files/{token}").status_code == 404
    assert client.get("/internal/files/guessed").status_code == 404


def test_internal_file_endpoint_refuses_public_clients(monkeypatch, tmp_path) -> None:
    app, client = _internal_client(monkeypatch, tmp_path, "8.8.8.8")
    token = app.state.runtime.file_tokens.issue("11111111-1111-1111-1111-111111111111")
    assert client.get(f"/internal/files/{token}").status_code == 404
