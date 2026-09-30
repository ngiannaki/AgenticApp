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

Reuses run_python_code's execution primitives (sandbox.py's throwaway
per-run directory, runner.py's timeout/env/output handling) so a spreadsheet
run has exactly the same isolation guarantees as any other code execution
here — the only difference is one extra file is pre-copied into the
sandbox before the script runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from backend.core.storage.document_store import DocumentStore
from backend.mcp_servers.code.errors import TimeoutError_, ValidationError
from backend.mcp_servers.code.execution.runner import _capture_artifacts, _clamp_timeout, _run, _sanitized_env
from backend.mcp_servers.code.execution.sandbox import isolated_workspace

_SPREADSHEET_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xls", ".ods"}
_LEGACY_ENGINES = {".xls": "xlrd", ".ods": "odf"}

_document_store = DocumentStore()


def _resolve_spreadsheet(document_id: str) -> Path:
    try:
        path = _document_store.get_file_path(document_id)
    except ValueError as exc:  # DocumentStore validates document_id as a UUID
        raise ValidationError(f"document_id={document_id!r} is not a valid document id.") from exc

    if path is None:
        raise ValidationError(f"No stored file found for document_id={document_id!r}.")
    if path.suffix.lower() not in _SPREADSHEET_EXTENSIONS:
        raise ValidationError(
            f"document_id={document_id!r} is a {path.suffix or '(no extension)'} file, "
            f"not a spreadsheet ({', '.join(sorted(_SPREADSHEET_EXTENSIONS))})."
        )
    return path


def profile_spreadsheet(document_id: str, *, settings: Any) -> dict[str, Any]:
    """Structural read of a spreadsheet: sheet names, row/column counts, column
    headers, and a few sample rows per sheet — no user code runs. This is the
    closest thing a spreadsheet has to a table of contents."""

    import pandas as pd

    path = _resolve_spreadsheet(document_id)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        df = pd.read_csv(path)
        sheets = [{
            "sheet": None,
            "rows": int(df.shape[0]),
            "columns": [str(c) for c in df.columns],
            "sample_rows": df.head(3).fillna("").to_dict(orient="records"),
        }]
        return {"success": True, "document_id": document_id, "file_type": "csv", "sheets": sheets}

    excel = pd.ExcelFile(path, engine=_LEGACY_ENGINES.get(suffix))
    sheets = []
    for name in excel.sheet_names:
        df = excel.parse(name)
        sheets.append({
            "sheet": name,
            "rows": int(df.shape[0]),
            "columns": [str(c) for c in df.columns],
            "sample_rows": df.head(3).fillna("").to_dict(orient="records"),
        })
    return {"success": True, "document_id": document_id, "file_type": suffix.lstrip("."), "sheets": sheets}


def run_python_code_on_document(
    document_id: str,
    code: str,
    *,
    settings: Any,
    timeout_seconds: Optional[int] = None,
    stdin: Optional[str] = None,
) -> dict[str, Any]:
    """Same execution model as run_python_code (fresh throwaway subprocess,
    stripped env, hard timeout), plus the spreadsheet's raw bytes copied into
    the sandbox before the script runs, at the relative path passed as
    sys.argv[1]."""

    if not code or not code.strip():
        raise ValidationError("code must be a non-empty string.")
    if len(code) > settings.max_code_length:
        raise ValidationError(f"code exceeds the {settings.max_code_length}-character limit.")

    source_path = _resolve_spreadsheet(document_id)
    timeout = _clamp_timeout(timeout_seconds, settings)
    env = _sanitized_env(settings)
    workspace_root = Path(settings.workspace_dir)

    with isolated_workspace(workspace_root, keep=settings.keep_workspace) as run_dir:
        data_filename = f"data{source_path.suffix.lower()}"
        (run_dir / data_filename).write_bytes(source_path.read_bytes())

        script_path = run_dir / "script.py"
        script_path.write_text(code, encoding="utf-8")
        argv = [settings.python_executable, "-I", str(script_path), data_filename]

        def _artifacts() -> list[dict[str, Any]]:
            return [a for a in _capture_artifacts(run_dir, settings) if a["filename"] != data_filename]

        try:
            result = _run(argv, cwd=run_dir, env=env, stdin=stdin, timeout_seconds=timeout, settings=settings)
        except TimeoutError_ as exc:
            exc.details["artifacts"] = _artifacts()
            raise
        result["artifacts"] = _artifacts()
        result["document_filename"] = data_filename
        return result
