"""The code sandbox's OS-level isolation: runner uids, resource limits,
cleanup, artifact capture, and the spreadsheet file hand-off.

The isolation tests need root on Linux (as in the code-sandbox container)
and an interpreter every runner uid can execute: SANDBOX_TEST_PYTHON, or
this test run's own interpreter. They're skipped otherwise. CI runs them
with sudo (see .github/workflows/ci.yml).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest

from backend.config.settings import CodeMcpSettings
from backend.mcp_servers.code.errors import UnsupportedOperationError, ValidationError
from backend.mcp_servers.code.execution import sandbox as sandbox_module
from backend.mcp_servers.code.execution import spreadsheet
from backend.mcp_servers.code.execution.runner import run_python_code
from backend.mcp_servers.code.execution.sandbox import isolated_run, uid_isolation_available

PYTHON = os.environ.get("SANDBOX_TEST_PYTHON", sys.executable)
needs_root = pytest.mark.skipif(not uid_isolation_available(), reason="uid isolation needs root on Linux")


def _pandas_available() -> bool:
    import subprocess

    return subprocess.run([PYTHON, "-c", "import pandas, openpyxl"], capture_output=True).returncode == 0


@pytest.fixture
def workspace():
    # Runner uids must be able to traverse every parent directory, so not
    # under pytest's root-only tmp_path.
    root = Path(tempfile.mkdtemp(prefix="sandbox-test-", dir="/var/tmp" if os.path.isdir("/var/tmp") else None))
    os.chmod(root, 0o711)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def settings(workspace, monkeypatch) -> CodeMcpSettings:
    # A fresh runner pool per test, on uids nothing else uses.
    monkeypatch.setattr(sandbox_module, "_pool", None)
    return CodeMcpSettings(
        python_executable=PYTHON,
        workspace_dir=str(workspace / "runs"),
        isolation="auto",
        runner_uid_start=29000 + (uuid.uuid4().int % 500) * 4,
        runner_uid_count=2,
        max_memory_mb=512,
        max_processes=16,
        default_timeout_seconds=20,
        max_timeout_seconds=20,
        file_source_url=None,
    )


def run(code: str, settings, **kwargs) -> dict:
    return run_python_code(code, settings=settings, **kwargs)


# --- isolation ----------------------------------------------------------------


@needs_root
def test_code_runs_as_an_unprivileged_runner_uid(settings) -> None:
    result = run("import os; print(os.getuid(), os.getgid(), os.getgroups())", settings)
    assert result["success"], result["stderr"]
    uid, gid, groups = result["stdout"].split(maxsplit=2)
    assert int(uid) >= settings.runner_uid_start and uid == gid and groups.strip() == "[]"


@needs_root
def test_code_cannot_read_the_servers_environment_or_root_files(settings, workspace, monkeypatch) -> None:
    monkeypatch.setenv("SANDBOX_TEST_SECRET", "s3cret-value")
    secret_file = workspace / "root-only.txt"
    secret_file.write_text("top secret")
    os.chmod(secret_file, 0o600)

    code = f"""
import os
print("env:", "SANDBOX_TEST_SECRET" in os.environ)
for target in ("/proc/{os.getpid()}/environ", {str(secret_file)!r}):
    try:
        open(target).read()
        print("READ", target)
    except PermissionError:
        print("denied", target)
"""
    result = run(code, settings)
    assert result["success"], result["stderr"]
    assert "env: False" in result["stdout"]
    assert "READ" not in result["stdout"] and result["stdout"].count("denied") == 2


@needs_root
def test_concurrent_runs_cannot_see_each_others_files(settings) -> None:
    with isolated_run(Path(settings.workspace_dir), settings) as (other_dir, other):
        (other_dir / "theirs.csv").write_text("private data")
        os.chown(other_dir / "theirs.csv", other.uid, other.uid)
        code = f"""
try:
    print(open({str(other_dir / 'theirs.csv')!r}).read())
except PermissionError:
    print("denied")
"""
        result = run(code, settings)
    assert result["stdout"].strip() == "denied"


@needs_root
def test_memory_and_process_limits(settings) -> None:
    memory = run("x = bytearray(1024 * 1024 * 1024)\nprint('allocated')", settings)
    assert not memory["success"] and "MemoryError" in memory["stderr"]

    forks = run(
        "import os, time\nn = 0\ntry:\n    for _ in range(200):\n        if os.fork() == 0:\n            time.sleep(30); os._exit(0)\n        n += 1\nexcept OSError:\n    pass\nprint(n)",
        settings,
    )
    assert int(forks["stdout"].split()[-1]) < 20


@needs_root
def test_leftover_processes_are_killed(settings) -> None:
    code = (
        "import os, subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], start_new_session=True)\n"
        "print(os.getuid())"
    )
    result = run(code, settings)
    uid = int(result["stdout"].split()[-1])
    still_running = [
        pid for pid in os.listdir("/proc")
        if pid.isdigit() and _proc_uid(pid) == uid
    ]
    assert still_running == []


def _proc_uid(pid: str) -> int | None:
    """Owner of a *live* process (zombies — dead, waiting to be reaped by
    init — don't count)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
        if state == "Z":
            return None
        return os.stat(f"/proc/{pid}").st_uid
    except (FileNotFoundError, IndexError, ProcessLookupError):
        return None


@needs_root
def test_artifacts_come_back_inline_and_symlinks_are_not_followed(settings) -> None:
    code = (
        "import os\n"
        "open('result.csv', 'w').write('a,b\\n1,2\\n')\n"
        "os.symlink('/etc/shadow', 'leak.txt')\n"
        "os.mkdir('subdir')\n"
    )
    result = run(code, settings)
    assert result["success"], result["stderr"]
    names = [a["filename"] for a in result["artifacts"]]
    assert names == ["result.csv"]
    assert base64.b64decode(result["artifacts"][0]["data_base64"]) == b"a,b\n1,2\n"
    assert "url" not in result["artifacts"][0]


@needs_root
def test_run_directory_is_removed_afterwards(settings) -> None:
    run("open('x.txt', 'w').write('x')", settings)
    assert list(Path(settings.workspace_dir).iterdir()) == []


# --- policy -------------------------------------------------------------------


def test_required_isolation_fails_closed(settings) -> None:
    unisolated = settings.model_copy(update={"isolation": "none", "require_isolation": True})
    with pytest.raises(UnsupportedOperationError):
        run("print(1)", unisolated)


# --- spreadsheet hand-off -----------------------------------------------------


def _mock_api(monkeypatch, responses: dict[str, httpx.Response]) -> list[str]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return responses.get(request.url.path.rsplit("/", 1)[-1], httpx.Response(404))

    real_client = httpx.Client
    monkeypatch.setattr(spreadsheet.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    return seen


def test_spreadsheet_is_fetched_with_the_one_time_token(settings, monkeypatch) -> None:
    remote = settings.model_copy(update={"file_source_url": "http://api.internal/internal/files"})
    seen = _mock_api(monkeypatch, {
        "tok-1": httpx.Response(200, content=b"a,b\n1,2\n", headers={"X-Document-Id": "doc-1", "X-Document-Suffix": ".csv"}),
        "tok-2": httpx.Response(200, content=b"x", headers={"X-Document-Id": "someone-elses", "X-Document-Suffix": ".csv"}),
    })
    assert spreadsheet._fetch_spreadsheet("doc-1", "tok-1", remote) == (".csv", b"a,b\n1,2\n")
    assert seen == ["http://api.internal/internal/files/tok-1"]

    with pytest.raises(ValidationError):  # token for a different document
        spreadsheet._fetch_spreadsheet("doc-1", "tok-2", remote)
    with pytest.raises(ValidationError):  # no token at all
        spreadsheet._fetch_spreadsheet("doc-1", "", remote)
    with pytest.raises(ValidationError):  # already used / unknown
        spreadsheet._fetch_spreadsheet("doc-1", "tok-3", remote)


@needs_root
@pytest.mark.skipif(not _pandas_available(), reason="runner interpreter has no pandas")
def test_profile_and_analysis_run_in_the_sandbox(settings, monkeypatch) -> None:
    remote = settings.model_copy(update={"file_source_url": "http://api.internal/internal/files"})
    csv = b"region,amount\nnorth,10\nsouth,32\nnorth,5\n"
    ok = {"X-Document-Id": "doc-1", "X-Document-Suffix": ".csv"}
    _mock_api(monkeypatch, {"t1": httpx.Response(200, content=csv, headers=ok), "t2": httpx.Response(200, content=csv, headers=ok)})

    profile = spreadsheet.profile_spreadsheet("doc-1", settings=remote, file_token="t1")
    assert profile["sheets"][0]["rows"] == 3 and profile["sheets"][0]["columns"] == ["region", "amount"]

    result = spreadsheet.run_python_code_on_document(
        "doc-1",
        "import sys, pandas as pd\ndf = pd.read_csv(sys.argv[1])\nprint(df.amount.sum())\ndf.to_csv('out.csv', index=False)",
        settings=remote,
        file_token="t2",
    )
    assert result["success"], result["stderr"]
    assert result["stdout"].strip() == "47"
    assert [a["filename"] for a in result["artifacts"]] == ["out.csv"]  # the input copy isn't echoed back
    json.dumps(result)  # result is plain JSON
