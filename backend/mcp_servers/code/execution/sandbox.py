"""Isolation for executed code.

Every call to run_python_code / run_python_code_on_document gets:

- its own throwaway working directory under the configured workspace root;
- when uid isolation is active (Linux, server running as root — the
  code-sandbox container): its own unprivileged runner uid, taken from a
  small pool and never shared by two runs at once. The run directory is
  owned by that uid with mode 0700, so concurrent runs (possibly different
  users) can't read each other's files, and the code can't read this
  server's own memory or /proc/<pid>/environ (different uid). After the
  run, every process left under that uid is killed before the directory is
  read back or removed;
- resource limits (address space, CPU seconds, file size, and — under uid
  isolation, where the count is per-user — number of processes), set by a
  tiny launcher right before it execs the real interpreter.

The container adds what a process can't do for itself: no network route
except to the API, read-only root filesystem, no secrets in the
environment (see docker/sandbox/Dockerfile and docker-compose.yml).
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from backend.mcp_servers.code.errors import UnsupportedOperationError

logger = logging.getLogger(__name__)

# Runs as the runner uid, before the user's code: lower the limits, then exec
# the real command. (Lowering limits needs no privilege; doing it here rather
# than in a preexec_fn keeps subprocess spawning thread-safe.)
_LAUNCHER = """\
import os, resource, sys
mem, cpu, fsize, nproc = (int(v) for v in sys.argv[1:5])
limits = [(resource.RLIMIT_AS, mem), (resource.RLIMIT_CPU, cpu), (resource.RLIMIT_FSIZE, fsize)]
if nproc > 0:
    limits.append((resource.RLIMIT_NPROC, nproc))
for res, value in limits:
    resource.setrlimit(res, (value, value))
os.execvp(sys.argv[5], sys.argv[5:])
"""


def uid_isolation_available() -> bool:
    return sys.platform.startswith("linux") and os.geteuid() == 0


@dataclass
class Isolation:
    """How one run is isolated. `uid` is None when running unisolated."""

    uid: Optional[int]
    settings: Any

    def wrap(self, argv: list[str]) -> list[str]:
        """Prefix `argv` with the rlimit launcher (POSIX only)."""
        if not hasattr(os, "fork"):
            return argv
        s = self.settings
        nproc = s.max_processes if self.uid is not None else 0
        limits = [
            s.max_memory_mb * 1024 * 1024,
            s.max_timeout_seconds + 5,  # CPU seconds; wall clock is enforced separately
            s.max_file_size_mb * 1024 * 1024,
            nproc,
        ]
        return [s.python_executable, "-I", "-c", _LAUNCHER, *map(str, limits), *argv]

    def popen_kwargs(self) -> dict[str, Any]:
        if sys.platform == "win32":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        kwargs: dict[str, Any] = {"process_group": 0}
        if self.uid is not None:
            kwargs.update(user=self.uid, group=self.uid, extra_groups=[])
        return kwargs


class RunnerPool:
    """Hands out runner uids, one run at a time per uid."""

    def __init__(self, start: int, count: int, python_executable: str) -> None:
        self._python = python_executable
        self._free: "queue.Queue[int]" = queue.Queue()
        for uid in range(start, start + count):
            self._free.put(uid)

    @contextmanager
    def acquire(self, wait_seconds: float) -> Iterator[int]:
        try:
            uid = self._free.get(timeout=wait_seconds)
        except queue.Empty:
            raise UnsupportedOperationError("The code sandbox is busy with other runs; try again shortly.") from None
        try:
            yield uid
        finally:
            kill_uid_processes(uid, self._python)
            self._free.put(uid)


_pool: Optional[RunnerPool] = None
_warned_unisolated = False


def _get_pool(settings: Any) -> RunnerPool:
    global _pool
    if _pool is None:
        _pool = RunnerPool(settings.runner_uid_start, settings.runner_uid_count, settings.python_executable)
    return _pool


def _isolation_mode(settings: Any) -> str:
    mode = (settings.isolation or "auto").lower()
    if mode == "auto":
        mode = "uid" if uid_isolation_available() else "none"
    if mode == "uid" and not uid_isolation_available():
        raise UnsupportedOperationError("uid isolation needs this server to run as root on Linux.")
    if mode == "none" and settings.require_isolation:
        raise UnsupportedOperationError(
            "Code execution is disabled: isolation is required (CODE_MCP_REQUIRE_ISOLATION) but not available."
        )
    return mode


def kill_uid_processes(uid: int, python_executable: str) -> None:
    """SIGKILL everything still running as `uid` (stragglers that left the
    process group, e.g. via setsid). kill(-1) from a process running as that
    uid reaches exactly that uid's processes — including itself, hence the
    expected -9 exit."""
    try:
        subprocess.run(
            [python_executable, "-I", "-c", "import os, signal; os.kill(-1, signal.SIGKILL)"],
            user=uid, group=uid, extra_groups=[], capture_output=True, timeout=10, check=False,
        )
    except Exception:  # noqa: BLE001 - best effort; the uid stays in the pool either way
        logger.exception("Could not clean up processes of runner uid %s", uid)


@contextmanager
def isolated_run(root: Path, settings: Any, *, keep: bool = False) -> Iterator[tuple[Path, Isolation]]:
    """A fresh run directory plus the isolation to run code in it with."""
    global _warned_unisolated
    mode = _isolation_mode(settings)
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / uuid.uuid4().hex

    if mode == "uid":
        # Runners may traverse the root to reach their own directory, but not list it.
        os.chmod(root, 0o711)
        with _get_pool(settings).acquire(settings.runner_wait_seconds) as uid:
            run_dir.mkdir(mode=0o700)
            os.chown(run_dir, uid, uid)
            try:
                yield run_dir, Isolation(uid=uid, settings=settings)
            finally:
                kill_uid_processes(uid, settings.python_executable)  # before anything reads the directory back
                if not keep:
                    shutil.rmtree(run_dir, ignore_errors=True)
        return

    if not _warned_unisolated:
        logger.warning("Running submitted code WITHOUT uid isolation (CODE_MCP_ISOLATION=%s).", settings.isolation)
        _warned_unisolated = True
    run_dir.mkdir()
    try:
        yield run_dir, Isolation(uid=None, settings=settings)
    finally:
        if not keep:
            shutil.rmtree(run_dir, ignore_errors=True)


def prepare_file(run_dir: Path, name: str, data: bytes, isolation: Isolation) -> Path:
    """Write a file into the run directory, owned by the run's uid."""
    path = run_dir / name
    path.write_bytes(data)
    if isolation.uid is not None:
        os.chown(path, isolation.uid, isolation.uid)
    return path


def read_regular_file(path: Path, max_bytes: int) -> Optional[bytes]:
    """Read a file the submitted code produced — but only if it is a plain
    regular file. A symlink (e.g. `plot.png -> /proc/1/environ` or another
    run's data) is refused rather than followed, since this read happens
    with the server's privileges."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read(max_bytes + 1)[:max_bytes]
    finally:
        if fd != -1:
            os.close(fd)
