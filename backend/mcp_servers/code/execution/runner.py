"""Subprocess execution core for the Code MCP server.

Every run happens in its own throwaway directory, as its own unprivileged
runner uid with resource limits when isolation is available (see
sandbox.py), with a stripped environment (only an explicit passthrough
allowlist reaches the child) and a hard wall-clock timeout. Nothing here
trusts the submitted code to behave: on timeout the whole process group is
killed, and under uid isolation every leftover process of that uid too.

Files the code leaves behind come back inline (base64) in the result,
never via this server's own disk: the API turns them into downloadable
artifacts (backend/apps/api/artifacts.py), so the sandbox container needs
no shared storage at all.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from backend.mcp_servers.code.artifacts import inline_artifact
from backend.mcp_servers.code.errors import TimeoutError_, UnsupportedOperationError, ValidationError
from backend.mcp_servers.code.execution.sandbox import Isolation, isolated_run, kill_uid_processes, read_regular_file


def _capture_artifacts(run_dir: Path, settings: Any, exclude: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Return the files the executed code left behind (besides script.py and
    `exclude`), e.g. a matplotlib plot saved with plt.savefig(...), inline as
    base64. Best-effort: a file over the size cap or anything that isn't a
    plain regular file (a symlink, a directory) is skipped, and this never
    raises — a capture problem shouldn't turn a successful run into a failed
    tool call."""

    if not settings.capture_artifacts:
        return []

    skip = {"script.py", *exclude}
    try:
        names = sorted(p.name for p in run_dir.iterdir() if p.name not in skip and not p.name.startswith("."))
    except OSError:
        return []

    artifacts: list[dict[str, Any]] = []
    for name in names:
        if len(artifacts) >= settings.max_artifacts_per_run:
            break
        data = read_regular_file(run_dir / name, settings.max_artifact_bytes)
        if data is not None:
            artifacts.append(inline_artifact(data, name))
    return artifacts


def _clamp_timeout(requested: Optional[int], settings: Any) -> int:
    if requested is None:
        return settings.default_timeout_seconds
    if requested <= 0:
        raise ValidationError("timeout_seconds must be a positive integer.")
    return min(requested, settings.max_timeout_seconds)


def _sanitized_env(settings: Any, run_dir: Optional[Path] = None) -> dict[str, str]:
    env = {name: os.environ[name] for name in settings.env_passthrough if name in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Keep numeric libraries from spawning a thread per core (each counts
    # against the process limit) and give caches (matplotlib's font cache)
    # somewhere writable that belongs to this run.
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("MPLBACKEND", "Agg")
    if run_dir is not None and os.name != "nt":
        # Home, caches and temp files all inside this run's private directory
        # (not a /tmp shared with other concurrent runs).
        env["HOME"] = str(run_dir)
        env["MPLCONFIGDIR"] = str(run_dir / ".matplotlib")
        env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(run_dir)
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
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the whole process group (process_group=0)
    except ProcessLookupError:
        pass
    except PermissionError:
        proc.kill()


def _drain(pipe, sink: list[str], keep_chars: int) -> None:
    """Read a pipe to EOF, keeping only the first `keep_chars` characters
    (the rest is read and dropped, so a chatty script can't block on a full
    pipe or fill this server's memory)."""
    kept = 0
    try:
        for chunk in iter(lambda: pipe.read(8192), ""):
            if kept < keep_chars:
                sink.append(chunk[: keep_chars - kept])
                kept += len(sink[-1])
    except (OSError, ValueError):
        pass


def _feed(pipe, data: Optional[str]) -> None:
    try:
        if data:
            pipe.write(data)
        pipe.close()
    except (BrokenPipeError, OSError, ValueError):
        pass


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdin: Optional[str],
    timeout_seconds: int,
    settings: Any,
    isolation: Isolation,
) -> dict[str, Any]:
    started = time.monotonic()
    proc = subprocess.Popen(
        isolation.wrap(argv), cwd=str(cwd), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        **isolation.popen_kwargs(),
    )
    # Collect output on background threads and wait on the process itself,
    # not on pipe EOF: a child the code left running (possibly in its own
    # session) can hold stdout/stderr open indefinitely, and must not stall
    # the result — or this server.
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    keep = settings.max_output_bytes + 1  # one past the cap, so _truncate still flags it
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out_chunks, keep), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_chunks, keep), daemon=True),
    ]
    for reader in readers:
        reader.start()
    threading.Thread(target=_feed, args=(proc.stdin, stdin), daemon=True).start()

    try:
        proc.wait(timeout=timeout_seconds)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
    # Whether it finished or not, nothing from this run may keep running:
    # the process group, plus (under uid isolation) every process of the
    # run's uid — that also catches children that left the group via setsid.
    _kill_tree(proc)
    if isolation.uid is not None:
        kill_uid_processes(isolation.uid, settings.python_executable)
    proc.wait()
    for reader in readers:
        reader.join(timeout=5)
    stdout, stderr = "".join(out_chunks), "".join(err_chunks)
    if any(reader.is_alive() for reader in readers):
        # Unisolated run whose escaped child still holds the pipe open.
        stderr += "\n[output may be incomplete: a background process kept it open]"

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
    workspace_root = Path(settings.workspace_dir)

    with isolated_run(workspace_root, settings, keep=settings.keep_workspace) as (run_dir, isolation):
        script_path = run_dir / "script.py"
        script_path.write_text(code, encoding="utf-8")
        if isolation.uid is not None:
            os.chown(script_path, isolation.uid, isolation.uid)
        env = _sanitized_env(settings, run_dir)
        # -I: isolated mode (ignores PYTHONPATH/user site dir) so the script
        # can't reach outside the sanitized env we just built.
        argv = [settings.python_executable, "-I", str(script_path), *(args or [])]
        try:
            result = _run(argv, cwd=run_dir, env=env, stdin=stdin, timeout_seconds=timeout, settings=settings, isolation=isolation)
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
    workspace_root = Path(settings.workspace_dir)

    with isolated_run(workspace_root, settings, keep=settings.keep_workspace) as (run_dir, isolation):
        env = _sanitized_env(settings, run_dir)
        try:
            result = _run(tokens, cwd=run_dir, env=env, stdin=stdin, timeout_seconds=timeout, settings=settings, isolation=isolation)
        except TimeoutError_ as exc:
            exc.details["artifacts"] = _capture_artifacts(run_dir, settings)
            raise
        result["artifacts"] = _capture_artifacts(run_dir, settings)
        return result
