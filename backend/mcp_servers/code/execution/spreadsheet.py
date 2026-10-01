"""Direct spreadsheet analysis: profiling and pandas execution against a raw
uploaded xlsx/csv, bypassing the indexing/RAG pipeline entirely.

Why this exists: retrieval over a spreadsheet indexes one chunk per row and
answers by similarity search over a capped top_k — it cannot guarantee
"every row matching X" or a trustworthy sum/count across all rows (see the
spreadsheet-data-strategy skill). A file attached directly in the chat inbox
(routers/files.py's session-upload branch) skips indexing for spreadsheet
extensions and is analyzed here instead: `profile_spreadsheet` gives a cheap
structural read (sheet names, sizes, columns) without running any code, and
`run_python_code_on_document` runs real pandas against the actual file for
anything that needs completeness or computation. Documents uploaded via the
standalone Documents page are unaffected — they still always go through
indexing, since that flow is about durable, cross-conversation search.

Reuses run_python_code's execution primitives (sandbox.py's per-run
directory and runner uid, runner.py's timeout/env/output handling) so a
spreadsheet run has exactly the same isolation guarantees as any other code
execution here — the only difference is one extra file is copied into the
run directory first. Profiling runs the same way (a fixed script, as the
runner uid): the server process itself never parses an uploaded file.

Where the file comes from: in the code-sandbox container
(CODE_MCP_FILE_SOURCE_URL set) the API puts a single-use `file_token` in the
call and this server downloads the bytes from the API's internal endpoint —
it has no access to the shared document folder. Without that setting
(non-container dev) it reads the local document store as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import httpx

from backend.mcp_servers.code.errors import TimeoutError_, ValidationError
from backend.mcp_servers.code.execution.runner import _capture_artifacts, _clamp_timeout, _run, _sanitized_env
from backend.mcp_servers.code.execution.sandbox import isolated_run, prepare_file

_SPREADSHEET_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xls", ".ods"}
_LEGACY_ENGINES = {".xls": "xlrd", ".ods": "odf"}
_MAX_DOCUMENT_BYTES = 100 * 1024 * 1024

# Run as the runner uid against sys.argv[1]; prints the profile as JSON.
_PROFILE_SCRIPT = """\
import json, sys
import pandas as pd

path = sys.argv[1]
engines = {".xls": "xlrd", ".ods": "odf"}
suffix = path[path.rfind("."):].lower()

def describe(df, name):
    return {
        "sheet": name,
        "rows": int(df.shape[0]),
        "columns": [str(c) for c in df.columns],
        "sample_rows": json.loads(df.head(3).fillna("").to_json(orient="records", date_format="iso")),
    }

if suffix == ".csv":
    sheets = [describe(pd.read_csv(path), None)]
else:
    excel = pd.ExcelFile(path, engine=engines.get(suffix))
    sheets = [describe(excel.parse(name), name) for name in excel.sheet_names]
print(json.dumps({"file_type": suffix.lstrip("."), "sheets": sheets}))
"""


def _check_suffix(document_id: str, suffix: str) -> str:
    suffix = (suffix or "").lower()
    if suffix not in _SPREADSHEET_EXTENSIONS:
        raise ValidationError(
            f"document_id={document_id!r} is a {suffix or '(no extension)'} file, "
            f"not a spreadsheet ({', '.join(sorted(_SPREADSHEET_EXTENSIONS))})."
        )
    return suffix


def _fetch_spreadsheet(document_id: str, file_token: str, settings: Any) -> tuple[str, bytes]:
    """(suffix, bytes) of the attached spreadsheet."""
    if settings.file_source_url:
        if not file_token:
            raise ValidationError("No file access was granted for this document (missing file_token).")
        url = f"{settings.file_source_url.rstrip('/')}/{file_token}"
        try:
            with httpx.Client(timeout=30.0, trust_env=False) as client:
                response = client.get(url)
        except httpx.HTTPError as exc:
            raise ValidationError(f"Could not fetch document {document_id!r}: {exc}") from exc
        if response.status_code != 200:
            raise ValidationError(f"Could not fetch document {document_id!r} (HTTP {response.status_code}).")
        if response.headers.get("X-Document-Id") != document_id:
            raise ValidationError("The file access granted doesn't match this document_id.")
        if len(response.content) > _MAX_DOCUMENT_BYTES:
            raise ValidationError("Document is too large to analyse.")
        return _check_suffix(document_id, response.headers.get("X-Document-Suffix", "")), response.content

    from backend.core.storage.document_store import DocumentStore

    try:
        path = DocumentStore().get_file_path(document_id)
    except ValueError as exc:  # DocumentStore validates document_id as a UUID
        raise ValidationError(f"document_id={document_id!r} is not a valid document id.") from exc
    if path is None:
        raise ValidationError(f"No stored file found for document_id={document_id!r}.")
    return _check_suffix(document_id, path.suffix), path.read_bytes()


def profile_spreadsheet(document_id: str, *, settings: Any, file_token: str = "") -> dict[str, Any]:
    """Structural read of a spreadsheet: sheet names, row/column counts, column
    headers, and a few sample rows per sheet — no user code runs. This is the
    closest thing a spreadsheet has to a table of contents."""

    suffix, data = _fetch_spreadsheet(document_id, file_token, settings)
    with isolated_run(Path(settings.workspace_dir), settings, keep=settings.keep_workspace) as (run_dir, isolation):
        data_filename = f"data{suffix}"
        prepare_file(run_dir, data_filename, data, isolation)
        script = prepare_file(run_dir, "script.py", _PROFILE_SCRIPT.encode("utf-8"), isolation)
        result = _run(
            [settings.python_executable, "-I", str(script), data_filename],
            cwd=run_dir, env=_sanitized_env(settings, run_dir), stdin=None,
            timeout_seconds=settings.max_timeout_seconds, settings=settings, isolation=isolation,
        )
    if not result["success"]:
        raise ValidationError(
            "Could not read the spreadsheet.", details={"stderr": result["stderr"][-2000:]}
        )
    try:
        profile = json.loads(result["stdout"].strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ValidationError("Could not read the spreadsheet profile.") from exc
    return {"success": True, "document_id": document_id, **profile}


def run_python_code_on_document(
    document_id: str,
    code: str,
    *,
    settings: Any,
    file_token: str = "",
    timeout_seconds: Optional[int] = None,
    stdin: Optional[str] = None,
) -> dict[str, Any]:
    """Same execution model as run_python_code (fresh run directory and
    runner uid, stripped env, hard timeout), plus the spreadsheet's raw
    bytes placed in the run directory before the script runs, at the
    relative path passed as sys.argv[1]."""

    if not code or not code.strip():
        raise ValidationError("code must be a non-empty string.")
    if len(code) > settings.max_code_length:
        raise ValidationError(f"code exceeds the {settings.max_code_length}-character limit.")

    suffix, data = _fetch_spreadsheet(document_id, file_token, settings)
    timeout = _clamp_timeout(timeout_seconds, settings)

    with isolated_run(Path(settings.workspace_dir), settings, keep=settings.keep_workspace) as (run_dir, isolation):
        data_filename = f"data{suffix}"
        prepare_file(run_dir, data_filename, data, isolation)
        script_path = prepare_file(run_dir, "script.py", code.encode("utf-8"), isolation)
        argv = [settings.python_executable, "-I", str(script_path), data_filename]
        env = _sanitized_env(settings, run_dir)

        try:
            result = _run(argv, cwd=run_dir, env=env, stdin=stdin, timeout_seconds=timeout, settings=settings, isolation=isolation)
        except TimeoutError_ as exc:
            exc.details["artifacts"] = _capture_artifacts(run_dir, settings, exclude=(data_filename,))
            raise
        result["artifacts"] = _capture_artifacts(run_dir, settings, exclude=(data_filename,))
        result["document_filename"] = data_filename
        return result
