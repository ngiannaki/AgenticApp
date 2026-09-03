"""Per-run isolated working directories for executed code.

Every call to run_python_code / run_shell_command gets its own throwaway
directory under the server's configured workspace root, so concurrent runs
never see each other's files and nothing persists after a run unless the
server is explicitly configured to keep it (debugging only).
"""

from __future__ import annotations

import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def isolated_workspace(root: Path, *, keep: bool = False) -> Iterator[Path]:
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        yield run_dir
    finally:
        if not keep:
            shutil.rmtree(run_dir, ignore_errors=True)
