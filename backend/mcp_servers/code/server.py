"""Code Execution MCP server.

Exposes tools over FastMCP (HTTP transport, mirroring the `indexing`,
`retrieval`, and `db` servers in this repo): run_python_code,
check_python_syntax, run_shell_command (disabled by default), generate_file,
profile_spreadsheet, run_python_code_on_document (the latter two operate on
a spreadsheet attached in the chat inbox — see execution/spreadsheet.py —
and are unrelated to the indexing/retrieval MCP servers' document pipeline).

Security model: submitted code always runs as a fresh subprocess in a
throwaway directory, as its own unprivileged runner uid with resource limits
(when this server runs as root on Linux — the code-sandbox container, see
execution/sandbox.py), with a stripped environment and a hard timeout —
never inside this server's own process. The container itself has no secrets,
a read-only filesystem and no network route except to the API. The one tool
that can reach an arbitrary executable (run_shell_command) is off by default
and, even when enabled, restricted to an explicit allowlist.

`file_token` on the spreadsheet tools is a single-use grant the API injects
(never chosen by the model) for downloading the attached file.
generate_file is a different animal entirely: it never executes anything,
only renders validated structured input into a file via python-docx /
openpyxl / the stdlib csv module. See README.md.
"""

import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# Add project root to Python path to support direct script execution
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from fastmcp import FastMCP

from backend.mcp_servers.code.config.settings import get_settings
from backend.mcp_servers.code.errors import to_error_dict
from backend.mcp_servers.code.execution.runner import run_python_code as _run_python_code
from backend.mcp_servers.code.execution.runner import run_shell_command as _run_shell_command
from backend.mcp_servers.code.execution.syntax import check_python_syntax as _check_python_syntax
from backend.mcp_servers.code.execution.spreadsheet import profile_spreadsheet as _profile_spreadsheet
from backend.mcp_servers.code.execution.spreadsheet import run_python_code_on_document as _run_python_code_on_document
from backend.mcp_servers.code.generation.generator import generate_file as _generate_file
from backend.mcp_servers.code.generation.ui import generate_ui as _generate_ui

logger = logging.getLogger(__name__)
handlers: list[logging.Handler] = [logging.StreamHandler()]
# Also log to a file when there's somewhere writable for it (not in the
# code-sandbox container, whose filesystem is read-only — stdout only there).
log_dir = Path(__file__).parent.parent.parent.parent / "logs"
try:
    log_dir.mkdir(exist_ok=True)
    handlers.append(logging.FileHandler(log_dir / "code_server.log", encoding="utf-8"))
except OSError:
    pass
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
root_logger = logging.getLogger()
for handler in handlers:
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)

mcp = FastMCP("code")

settings = get_settings()


def _handle(tool_name: str, fn, *args, **kwargs) -> dict[str, Any]:
    """Shared call wrapper: request-id tracing, timing, and error translation.

    Every tool goes through this so that (a) unexpected exceptions never
    leak internals to the agent and (b) we get consistent structured logs
    without duplicating the same try/except/log boilerplate across tools.
    """

    request_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    logger.info("[%s] request_id=%s", tool_name, request_id)

    try:
        result = fn(*args, **kwargs)
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            "[%s] request_id=%s success elapsed_ms=%.1f exit_code=%s",
            tool_name, request_id, elapsed_ms, result.get("exit_code"),
        )
        return result
    except Exception as exc:  # noqa: BLE001 - translated centrally, never re-raised raw
        category = getattr(exc, "category", "execution_error")
        logger.warning("[%s] request_id=%s failed category=%s error=%s", tool_name, request_id, category, exc)
        return to_error_dict(exc)


@mcp.tool
def run_python_code(
    code: str,
    timeout_seconds: Optional[int] = None,
    stdin: Optional[str] = None,
    args: Optional[list[str]] = None,
) -> dict:
    """Run Python code and return whether it succeeded plus its output.

    Writes `code` to a script in a fresh, throwaway temp directory and runs
    it with the server's Python interpreter as a real subprocess — never
    inside this server's own process, and never with access to this
    backend's environment variables (API keys, DB URLs, etc. are not
    inherited). Use this to actually execute and test code, not just check
    that it parses (see check_python_syntax for that).

    Returns `{success, exit_code, stdout, stderr, timed_out, truncated,
    elapsed_ms, artifacts}`. `success` is true iff the process exited with
    code 0 — check `stderr` for a traceback when it's false. Output is
    capped in size (see `truncated`) and execution is capped at
    `timeout_seconds` (default and max are server-configured); a run that
    hits the timeout is killed, including any child processes it spawned,
    and reported as a `timeout_error` (with whatever partial stdout/stderr
    was captured).

    Any files the code wrote into its working directory — e.g. a plot saved
    with `plt.savefig("plot.png")` — are picked up automatically and come
    back in `artifacts` as a list of `{filename, url, content_type,
    size_bytes}`. To show a plot to the user, save it to a file in the
    current directory (a relative path, no `plt.show()` — there is no
    display) and then reference the returned `url` — e.g. markdown
    `![plot](url)` — in your reply so the chat UI renders it inline.

    `args` become sys.argv[1:] for the script; `stdin` is piped to the
    process if the code reads from standard input.
    """
    return _handle(
        "run_python_code", _run_python_code, code,
        settings=settings, timeout_seconds=timeout_seconds, stdin=stdin, args=args,
    )


@mcp.tool
def check_python_syntax(code: str) -> dict:
    """Check whether Python code is syntactically valid, without running it.

    Fast and side-effect-free (uses `ast.parse`, no subprocess is started).
    Use this to validate code before run_python_code, or to pinpoint a
    syntax error's exact line/column without spending a full execution. On
    failure returns `{success: false, valid: false, error, line, column,
    text}`; on success `{success: true, valid: true}`. A clean result here
    only means the code parses — it says nothing about whether it will run
    without errors, so still call run_python_code to actually test it.
    """
    return _handle("check_python_syntax", _check_python_syntax, code, settings=settings)


#@mcp.tool
def run_shell_command(
    command: str,
    timeout_seconds: Optional[int] = None,
    stdin: Optional[str] = None,
) -> dict:
    """Run a shell command (e.g. `pip install requests`, `pytest -q`) and return its output.

    Disabled by default (`CODE_MCP_ALLOW_SHELL_COMMANDS=false`) — this has a
    much larger blast radius than run_python_code, since it can reach any
    executable rather than just this server's Python interpreter. When
    enabled, only commands whose first token is in the server-configured
    allowlist (`CODE_MCP_ALLOWED_SHELL_EXECUTABLES`, default: python, pip,
    pytest) are permitted; anything else is rejected as
    `unsupported_operation` before a process is ever started.

    Same throwaway-temp-directory, stripped-environment, hard-timeout
    execution model as run_python_code. Returns the same
    `{success, exit_code, stdout, stderr, timed_out, truncated, elapsed_ms}`
    shape.
    """
    return _handle(
        "run_shell_command", _run_shell_command, command,
        settings=settings, timeout_seconds=timeout_seconds, stdin=stdin,
    )


@mcp.tool
def profile_spreadsheet(document_id: str, file_token: str = "") -> dict:
    """Inspect a spreadsheet attached in this chat (xlsx/xlsm/xls/ods/csv)
    without running any code: sheet names, row/column counts, column headers,
    and 3 sample rows per sheet. Use this first for a spreadsheet the user
    just attached — it's the equivalent of a table of contents for tabular
    data, and tells you what's actually in the file (how many sheets, how
    big each one is, what the columns are called) before you write any
    analysis code.

    `document_id` must be a spreadsheet stored via the chat file-attach flow
    (not a Documents-page upload — those are indexed instead, and are read
    with retrieval_retrieve_document_v2 / retrieval_get_table_of_contents_v2,
    not this tool).

    Returns `{success, document_id, file_type, sheets: [{sheet, rows,
    columns, sample_rows}, ...]}`. `sheet` is null for csv (single implicit
    sheet). Unlike retrieval over an indexed document, `rows` here is the
    real, exact row count for that sheet — not a sampled estimate.
    """
    return _handle("profile_spreadsheet", _profile_spreadsheet, document_id, settings=settings, file_token=file_token)


@mcp.tool
def run_python_code_on_document(
    document_id: str,
    code: str,
    timeout_seconds: Optional[int] = None,
    stdin: Optional[str] = None,
    file_token: str = "",
) -> dict:
    """Run pandas (or any Python) code against the full, real contents of a
    spreadsheet attached in this chat — for anything profile_spreadsheet's
    structural summary can't answer: exact sums/averages/counts, filtering
    every row that matches a condition, joins across sheets, reshaping,
    plotting. Use this instead of trying to answer a "sum this column" or
    "every row where X" question from a partial view of the data — that's
    exactly the completeness problem this tool exists to solve.

    Same isolation model as run_python_code (fresh throwaway subprocess,
    stripped environment, hard timeout) plus one extra thing: the
    spreadsheet's raw bytes are copied into the sandbox before your script
    runs, at the relative path passed to it as `sys.argv[1]` — read it with
    e.g. `pandas.read_excel(sys.argv[1], sheet_name=None)` (all sheets) or
    `pandas.read_csv(sys.argv[1])`. Don't hardcode a filename; always read it
    from argv.

    `document_id` must be a spreadsheet stored via the chat file-attach flow
    (see profile_spreadsheet). Returns the same
    `{success, exit_code, stdout, stderr, timed_out, truncated, elapsed_ms,
    artifacts}` shape as run_python_code — print() what you need to answer
    the question, since stdout is what comes back to you. Saving a file
    (e.g. `plt.savefig("plot.png")`) works the same way too: it comes back
    in `artifacts` with a URL to reference in your reply.
    """
    return _handle(
        "run_python_code_on_document", _run_python_code_on_document, document_id, code,
        settings=settings, timeout_seconds=timeout_seconds, stdin=stdin, file_token=file_token,
    )


@mcp.tool
def generate_file(format: str, filename: str, content: dict) -> dict:
    """Generate a properly-formatted txt, md, csv, xlsx, or docx file from
    structured content and return a URL the user can open it at.

    Unlike run_python_code, nothing is executed — `content` is validated,
    structured data that gets rendered directly (python-docx for docx,
    openpyxl for xlsx, the stdlib csv module for csv). Use this whenever the
    goal is "produce a document/spreadsheet", even a simple one — it
    produces real formatting (Word heading styles and bordered tables, bold
    shaded Excel header rows with auto-sized columns, a proper Markdown
    table, BOM-prefixed CSV that opens correctly in Excel) with far less
    effort and far fewer ways to fail than writing that formatting by hand
    in run_python_code.

    `filename` is the base name; the correct extension is appended
    automatically if missing.

    `content`'s shape depends on `format`:

    For **txt** / **md** / **docx** (a "document" — title + sections):
    ```
    {
      "title": "Optional overall title",
      "sections": [
        {
          "heading": "Optional section heading",
          "level": 1,                          # 1-4, heading depth
          "paragraphs": ["Plain prose paragraphs, one string each."],
          "bullets": ["Unordered list items."],
          "numbered": ["Ordered list items."],
          "table": {"headers": ["Col A", "Col B"], "rows": [["1", "2"]]}
        }
      ]
    }
    ```
    At least one of `title` / `sections` is required, and each section needs
    at least one of heading/paragraphs/bullets/numbered/table. Every table
    row must have as many cells as `headers` (or as the first row, if there
    are no headers).

    For **csv** (a single table):
    ```
    {"headers": ["Name", "Revenue"], "rows": [["Acme", 1200], ["Globex", 950]]}
    ```
    `headers` is optional; `rows` is required. Cell values may be strings,
    numbers, booleans, or null.

    For **xlsx** (one table, or several named sheets):
    ```
    {"headers": [...], "rows": [[...], ...]}                              # single sheet
    {"sheets": [{"name": "Q1", "headers": [...], "rows": [[...]]}, ...]}   # multiple
    ```
    Same per-row rules as csv; a sheet name is truncated to Excel's 31-char
    limit. Numbers stay numbers in the spreadsheet, not text.

    Returns `{success, filename, url, content_type, size_bytes}` on success.
    To show the result to the user, reference the returned `url` in your
    reply (e.g. markdown `[report.xlsx](url)`, or `![plot](url)` for an
    image) so the chat UI can render or link it.
    """
    return _handle("generate_file", _generate_file, format, filename, content, settings=settings)


if settings.allow_ui_generation:

    @mcp.tool
    def generate_ui(html: str, title: str = "Generated UI") -> dict:
        """Generate a custom, interactive UI component from HTML/CSS/JS you write
        yourself, and return a URL the chat UI renders inline in a sandboxed iframe.

        Use this when the user wants something to actually see and interact with —
        a form, a mini dashboard, a calculator, an interactive diagram, a game —
        rather than a plain text/markdown answer. There is no fixed component
        library here: write real HTML with inline `<style>`/`<script>` (or a
        `<script src="https://...">` from a public CDN) exactly as you would for
        a real webpage. Keep it self-contained — everything the page needs should
        be in `html` itself or loaded from a public URL, since it will run
        completely isolated from this conversation and from the rest of the app
        (no access to cookies, local storage, or any other page's data).

        For charts and dashboards specifically: prefer self-contained inline
        SVG + vanilla JS over pulling in an external charting library (Plotly,
        Chart.js, etc.) via CDN. The sandbox has no storage access at all (see
        above), and most charting libraries touch localStorage/sessionStorage
        somewhere in their init path — that throws in this sandbox and silently
        halts *all* script execution on the page before anything renders, with
        no console visible to the user to explain why it's blank. Plain SVG
        elements plus a `<script>` you write yourself for hover tooltips
        (mouseover/mouseout on each shape), legend filtering (toggle a series'
        visibility on click), and control inputs (a `<select>`/slider that
        redraws on `change`) has no such dependency and reliably works — it's
        also usually less code than wiring up a charting library for the same
        result. Reach for a CDN library only for something that specifically
        needs it (e.g. a physics/game engine), not for charts.

        The page runs in a browser iframe sandbox with scripts allowed but no
        access to the parent page or its storage, so: don't rely on `window.top`,
        `postMessage` to a parent you don't control, or reading/writing outside
        the iframe. If the UI needs to report something back to the conversation
        (e.g. a form's submitted values), have the user copy/paste it, or just
        describe what happened in your next reply — there is no live channel back
        to you once the iframe is rendered.

        `html` may be a full document (starting with `<!doctype html>` or
        `<html>`) or just a body fragment — a fragment is automatically wrapped
        in a minimal document with a UTF-8 charset and a responsive viewport meta
        tag. `title` becomes the document's `<title>` (used only if `html` is a
        fragment); defaults to "Generated UI".

        Returns `{success, filename, url, content_type, size_bytes}` on success.
        The chat UI automatically renders this result as an embedded
        preview/code panel (with a live iframe preview and a "Code" tab
        showing the raw HTML) as soon as the tool call completes — you do not
        need to reference the `url` in your reply for it to show up. Don't
        repeat the raw URL as a markdown link in your answer; just describe
        what you built in a sentence or two.

        This tool can be turned off by an operator at any time (CODE_MCP_ALLOW_UI_GENERATION=false
        in the server's config) to stop generative-UI usage — if it isn't in
        your tool list, it's currently disabled; don't ask the user to enable it,
        just answer without it.
        """
        return _handle("generate_ui", _generate_ui, html, title, settings=settings)


if __name__ == "__main__":
    import os

    # MCP_TRANSPORT=stdio lets this server be launched as a subprocess
    # (e.g. from servers.docker.json) instead of a standalone HTTP service —
    # see backend/apps/client/servers.docker.json and the root Dockerfile.
    if os.environ.get("MCP_TRANSPORT", "http") == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="http",
            host=settings.host,
            port=settings.port,
        )
