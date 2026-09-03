"""Subprocess execution core for the Code MCP server.

Every run happens in its own throwaway temp directory (see sandbox.py), with
a stripped environment (only an explicit passthrough allowlist reaches the
child — this backend's secrets in `backend/.env` are never inherited) and a
hard wall-clock timeout. Nothing here trusts the submitted code to behave:
on timeout, the whole process tree is killed, not just the top process.
"""

from __future__ import annotations

import mimetypes
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from backend.mcp_servers.code.artifacts import artifact_url, get_artifact_store
from backend.mcp_servers.code.errors import TimeoutError_, UnsupportedOperationError, ValidationError
from backend.mcp_servers.code.execution.sandbox import isolated_workspace


def _capture_artifacts(run_dir: Path, settings: Any) -> list[dict[str, Any]]:
    """Persist any files the executed code left behind (besides script.py)
    so they can be served back to the UI, e.g. a matplotlib plot saved with
    plt.savefig(...). Best-effort: a file over the size cap is skipped, not
    truncated, and this never raises — a capture problem shouldn't turn a
    successful run into a failed tool call."""

    if not settings.capture_artifacts:
        return []

    generated = sorted(p for p in run_dir.iterdir() if p.is_file() and p.name != "script.py")
    if not generated:
        return []

    store = get_artifact_store()
    artifacts: list[dict[str, Any]] = []
    for path in generated[: settings.max_artifacts_per_run]:
        size = path.stat().st_size
        if size > settings.max_artifact_bytes:
            continue
        artifact_id = store.save(path.read_bytes(), path.name)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        artifacts.append({
            "filename": path.name,
            "url": artifact_url(settings, artifact_id, path.name),
            "content_type": content_type,
            "size_bytes": size,
        })
    return artifacts


def _clamp_timeout(requested: Optional[int], settings: Any) -> int:
    if requested is None:
        return settings.default_timeout_seconds
    if requested <= 0:
        raise ValidationError("timeout_seconds must be a positive integer.")
    return min(requested, settings.max_timeout_seconds)


def _sanitized_env(settings: Any) -> dict[str, str]:
    env = {name: os.environ[name] for name in settings.env_passthrough if name in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return text, False
    return data[:max_bytes].decode("utf-8", errors="ignore") + "\n...[output truncated]", True


def _kill_tree(proc: "subprocess.Popen[str]") -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, check=False,
        )
    else:
        proc.kill()


def _run(argv: list[str], *, cwd: Path, env: dict[str, str], stdin: Optional[str], timeout_seconds: int, settings: Any) -> dict[str, Any]:
    started = time.monotonic()
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(input=stdin, timeout=timeout_seconds)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        stdout, stderr = proc.communicate()
        timed_out = True

    elapsed_ms = (time.monotonic() - started) * 1000
    stdout, stdout_truncated = _truncate(stdout or "", settings.max_output_bytes)
    stderr, stderr_truncated = _truncate(stderr or "", settings.max_output_bytes)

    if timed_out:
        raise TimeoutError_(
            f"Execution exceeded the {timeout_seconds}s timeout and was terminated.",
            details={"stdout": stdout, "stderr": stderr, "elapsed_ms": round(elapsed_ms, 1)},
        )

    return {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": False,
        "truncated": stdout_truncated or stderr_truncated,
        "elapsed_ms": round(elapsed_ms, 1),
    }


def run_python_code(
    code: str,
    *,
    settings: Any,
    timeout_seconds: Optional[int] = None,
    stdin: Optional[str] = None,
    args: Optional[list[str]] = None,
) -> dict[str, Any]:
    if not code or not code.strip():
        raise ValidationError("code must be a non-empty string.")
    if len(code) > settings.max_code_length:
        raise ValidationError(f"code exceeds the {settings.max_code_length}-character limit.")

    timeout = _clamp_timeout(timeout_seconds, settings)
    env = _sanitized_env(settings)
    workspace_root = Path(settings.workspace_dir)

    with isolated_workspace(workspace_root, keep=settings.keep_workspace) as run_dir:
        script_path = run_dir / "script.py"
        script_path.write_text(code, encoding="utf-8")
        # -I: isolated mode (ignores PYTHONPATH/user site dir) so the script
        # can't reach outside the sanitized env we just built.
        argv = [settings.python_executable, "-I", str(script_path), *(args or [])]
        try:
            result = _run(argv, cwd=run_dir, env=env, stdin=stdin, timeout_seconds=timeout, settings=settings)
        except TimeoutError_ as exc:
            exc.details["artifacts"] = _capture_artifacts(run_dir, settings)
            raise
        result["artifacts"] = _capture_artifacts(run_dir, settings)
        return result


def run_shell_command(
    command: str,
    *,
    settings: Any,
    timeout_seconds: Optional[int] = None,
    stdin: Optional[str] = None,
) -> dict[str, Any]:
    if not settings.allow_shell_commands:
        raise UnsupportedOperationError("Shell command execution is disabled (CODE_MCP_ALLOW_SHELL_COMMANDS=false).")
    if not command or not command.strip():
        raise ValidationError("command must be a non-empty string.")
    if len(command) > settings.max_code_length:
        raise ValidationError(f"command exceeds the {settings.max_code_length}-character limit.")

    try:
        tokens = shlex.split(command, posix=(sys.platform != "win32"))
    except ValueError as exc:
        raise ValidationError(f"Could not parse command: {exc}") from exc
    if not tokens:
        raise ValidationError("command must be a non-empty string.")

    executable = Path(tokens[0]).stem.lower()
    allowed = {e.lower() for e in settings.allowed_shell_executables}
    if executable not in allowed:
        raise UnsupportedOperationError(
            f"'{tokens[0]}' is not in the allowed executables list ({', '.join(settings.allowed_shell_executables)})."
        )

    timeout = _clamp_timeout(timeout_seconds, settings)
    env = _sanitized_env(settings)
    workspace_root = Path(settings.workspace_dir)

    with isolated_workspace(workspace_root, keep=settings.keep_workspace) as run_dir:
        try:
            result = _run(tokens, cwd=run_dir, env=env, stdin=stdin, timeout_seconds=timeout, settings=settings)
        except TimeoutError_ as exc:
            exc.details["artifacts"] = _capture_artifacts(run_dir, settings)
            raise
        result["artifacts"] = _capture_artifacts(run_dir, settings)
        return result
