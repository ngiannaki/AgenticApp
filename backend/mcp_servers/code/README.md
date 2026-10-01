# Code Execution MCP Server

A FastMCP server with three distinct jobs: **running code** (submit Python,
get back exit code/stdout/stderr/timing), **generating files**
(txt/md/csv/xlsx/docx from structured input, no code required, real
formatting), and **generating UI** (model-authored HTML/CSS/JS rendered
inline in the chat UI's sandboxed iframe). Part of this repo's MCP stack,
alongside the `indexing`, `retrieval`, and `db` servers in
`backend/mcp_servers/`.

## Why this isn't "just call subprocess.run from a tool"

Submitted code is, by definition, not trusted — an agent might hallucinate
something destructive, or a prompt injection might try to steer it into
one. So every run goes through the same pipeline regardless of what the
code says:

1. **Never executes in this server's own process.** Code is written to a
   file and run as a real subprocess with `python -I` (isolated mode).
2. **Runs in a throwaway temp directory** (`execution/sandbox.py`), unique
   per call, deleted afterward unless `CODE_MCP_KEEP_WORKSPACE=true`.
3. **Gets a stripped environment**, not this process's environment. Only an
   explicit allowlist of host env vars (`CODE_MCP_ENV_PASSTHROUGH`, default
   `PATH`/`SYSTEMROOT`/`TEMP`/`TMP`/`PATHEXT`) is forwarded — this backend's
   `.env` secrets (API keys, DB URLs, Mongo connection string, ...) are
   never visible to `os.environ` inside the executed code.
4. **Has a hard wall-clock timeout**, clamped to a server-configured
   ceiling regardless of what the caller asks for. On timeout the whole
   process tree is killed (`taskkill /F /T` on Windows, process kill on
   POSIX) — not just the top-level interpreter — so a script that spawned
   children can't outlive its timeout.
5. **Output is size-capped** (`CODE_MCP_MAX_OUTPUT_BYTES`); a run that
   produces more stdout/stderr than that is truncated, never buffered
   without bound.
6. **Runs as its own unprivileged user, with resource limits** (Linux, when
   this server runs as root — the `code-sandbox` container). Each run takes
   a uid from a small pool (`CODE_MCP_RUNNER_UID_START`/`_COUNT`) that no
   other concurrent run shares; its directory is `0700` and owned by that
   uid, so two users' runs can't read each other's files, and the code can't
   read this server's memory or `/proc/*/environ`. A launcher sets address
   space / CPU / file-size / process-count limits before exec. When the run
   ends, every process still running as that uid is killed — including ones
   that escaped the process group — before the directory is read back.
   `CODE_MCP_REQUIRE_ISOLATION=true` (set in the image) refuses to run code
   at all if this isolation isn't available.

## The container (`docker/sandbox/Dockerfile`)

The image holds only this server and the libraries submitted code may use
(pandas, numpy, matplotlib, openpyxl, ...) — no other server code, no
`backend/.env`, no secrets. docker-compose.yml runs it with a read-only root
filesystem (tmpfs at `/work` and `/tmp`), all capabilities dropped except
the five needed to start runs as other uids, `no-new-privileges`,
memory/pids/cpu limits, `init` to reap stray processes, and on an
`internal` network shared only with the API: no internet, no Postgres.

Attached spreadsheets reach it without any shared disk: the API injects a
single-use `file_token` into each spreadsheet tool call (the model never
sees that parameter), and this server downloads the file once from the
API's `/internal/files/{token}` (`CODE_MCP_FILE_SOURCE_URL`). Generated
files go back the same way in reverse — inline in the tool result — see
below. `docker/sandbox/smoke_test.py` checks a running container.

## Setup

1. **No extra dependencies to install.** `run_python_code` uses only the
   standard library (`subprocess`, `ast`, `shlex`); `generate_file` uses
   `python-docx` and `openpyxl`, which are already installed in this repo's
   venv (used elsewhere for document parsing). See `requirements.txt`.
2. **Configure** (optional — every setting has a working default):
   ```
   # edit backend/.env — see the CODE_MCP_* section in backend/.env.example
   ```
3. **Run the server** (matches how `indexing`/`retrieval`/`db` are started;
   see the repo's `start_mcp_servers.ps1` / `RUNNING.md`):
   ```
   .venv/Scripts/python.exe backend/mcp_servers/code/server.py
   ```
   It listens on `http://0.0.0.0:8003/mcp` by default (`CODE_MCP_PORT`).
4. **Point a client at it.** `backend/apps/client/servers.json` already
   registers `"code": {"url": "http://localhost:8003/mcp"}`; tools appear
   namespaced as `code_run_python_code`, `code_generate_file`, etc.

## The four tools

| Tool | Accepts | Purpose |
|---|---|---|
| `run_python_code` | Python source (+ optional args/stdin/timeout) | Actually executes it, returns exit code + stdout/stderr + any generated files |
| `check_python_syntax` | Python source | Fast `ast.parse`-based syntax check, no execution |
| `run_shell_command` | a command line | Runs an allowlisted executable (disabled by default) |
| `generate_file` | structured content + a format | Renders a properly-formatted txt/md/csv/xlsx/docx — no code involved |
| `generate_ui` | free-form HTML/CSS/JS | Saves a self-contained webpage as an artifact; the frontend embeds it in a sandboxed iframe — a real "generative UI" surface, not a fixed component library |

`run_python_code` is the primary code-running tool — "does this code work"
is answered by `success` (exit code 0) plus `stderr` for a traceback when it
isn't. `check_python_syntax` is a cheap pre-check (no subprocess, can't time
out, can't have side effects) for catching a typo before spending a full
run. `generate_file` is a separate, unrelated capability — see below.

## `generate_file`: documents/spreadsheets without writing code

For "produce a report/spreadsheet", making the agent write correct
`python-docx`/`openpyxl` code through `run_python_code` every time is
needlessly failure-prone. `generate_file` instead takes validated
structured content and a `format` (`txt`/`md`/`csv`/`xlsx`/`docx`) and
renders it directly — same artifact-capture and `GET /api/files/{id}/...`
delivery path as `run_python_code`'s generated files (see below), just
without a subprocess, sandbox, or timeout in the picture at all: content is
either valid or it's rejected with a `validation_error` before anything is
written.

Two content schemas, depending on format (see the tool's docstring for the
full shape and examples):
- **txt / md / docx** — a "document": optional `title` + a list of
  `sections`, each an optional mix of heading, paragraphs, a bullet list, a
  numbered list, and a table. Rendered as real Word heading styles and
  bordered tables (docx), a proper heading hierarchy and pipe tables (md),
  or plain aligned text (txt) — not the same code path with three
  extensions swapped.
- **csv / xlsx** — a "table": `headers` (optional) + `rows`, or for xlsx
  only, multiple named `sheets`. xlsx gets a bold shaded header row, frozen
  header pane, and auto-sized columns; csv gets a UTF-8 BOM so Excel opens
  non-ASCII text correctly instead of mangling it. Cell values keep their
  real type (a number stays a number, not text that looks like one).

Filenames are sanitized to a bare basename (`Path(name).name`) before
anything touches disk, so a path-traversal attempt in `filename` (e.g.
`"../../evil"`) can't escape the artifact store — it's silently reduced to
just `evil.<ext>`.

## How generated files reach the UI

Both tools that can produce a file — `run_python_code` (anything the code
writes into its working directory besides `script.py`) and `generate_file`
(always) — return it **inline** in the tool result: `{filename,
content_type, size_bytes, data_base64}` (`run_python_code` nests a list of
these under `artifacts`; `generate_file`/`generate_ui` return the fields
directly). The API (`backend/apps/api/artifacts.py`) stores each one in its
`ArtifactStore` (`ARTIFACT_STORE_DIR`, default `data/artifacts`) and swaps
the bytes for a `url` before the model, the UI or the chat history see the
result. So `plt.savefig("plot.png")` in `run_python_code` (no `plt.show()` —
there's no display) works the same way a `generate_file` call does: the
agent references the returned `url` in its reply (e.g. markdown
`![plot](url)` or `[report.xlsx](url)`) and the chat UI renders or links
it inline. This server keeps nothing on disk between calls.

Stored artifacts are **not** cleaned up automatically, since the point is
for the UI to be able to fetch them after the call has finished. They're
served by the Agentic API at `GET /api/files/{artifact_id}/{filename}`
(`backend/apps/api/routers/files.py`), with a `sandbox` Content Security
Policy so a generated page can't act as the app.

Controlled by:
- `CODE_MCP_CAPTURE_ARTIFACTS` (default `true`) — master switch for
  `run_python_code`'s automatic capture (`generate_file` always saves its
  output; there's nothing to "capture" there).
- `CODE_MCP_MAX_ARTIFACT_BYTES` (default 10 MB) — a `run_python_code`
  output file over this is skipped, not truncated; a `generate_file` result
  over this is rejected outright (`validation_error`) instead of being
  silently dropped, since there's no run to otherwise report success on.
- `CODE_MCP_MAX_ARTIFACTS_PER_RUN` (default 5) — `run_python_code` only.
- `CODE_MCP_ARTIFACT_PUBLIC_BASE_URL` (default `http://localhost:8100`) —
  read by the **API**: where it is reachable from a browser; the returned
  `url` is built from this. Point it at nginx (e.g. `https://localhost:8443`) if you're
  not running the API directly on 8100.

Capture only ever looks at top-level entries of the run's own directory,
and only reads plain regular files without following symlinks (a
`plot.png -> /etc/shadow` link is skipped, since the read happens with this
server's privileges).

## `generate_ui`: free-form, LLM-authored UI, not a fixed component set

Unlike a JSON-spec-driven UI (the model picks from components you predefined),
`generate_ui` takes real HTML/CSS/JS the model writes itself — a custom form,
a mini dashboard, a game, anything — and hands back a URL, same
`ArtifactStore` + `GET /api/files/{id}/{filename}` path as `generate_file`.
The frontend (`ChatWindow.tsx`'s `UiArtifactBlock`) embeds that URL in an
`<iframe sandbox="allow-scripts allow-forms">` — no `allow-same-origin`, so
the generated page can't read the parent app's cookies, storage, or DOM even
if the generated JS is buggy or hostile. That sandbox, not anything on the
server side, is the actual security boundary; `generate_ui` itself only
size-caps the input (`CODE_MCP_MAX_UI_HTML_LENGTH`) and wraps a bare fragment
in a minimal document (UTF-8 charset, responsive viewport meta) if it isn't
already a full `<html>` document.

**Kill switch:** `CODE_MCP_ALLOW_UI_GENERATION` (default `true`). Set to
`false` and restart the server to remove `generate_ui` from the tool list
entirely — the model never sees it, so it can't be overused. This is a
server-config switch (like `CODE_MCP_ALLOW_SHELL_COMMANDS`), not a per-user
toggle; it's the deliberate operator-facing control for "generative UI is
getting used more than we want."

## `run_shell_command`: off by default, allowlisted when on

Arbitrary shell access is a much larger blast radius than sandboxed Python
(it can reach any installed executable, not just this server's
interpreter), so it's gated behind two independent switches:

- `CODE_MCP_ALLOW_SHELL_COMMANDS` (default `false`) — the master switch.
- `CODE_MCP_ALLOWED_SHELL_EXECUTABLES` (default `["python", "pip",
  "pytest"]`) — even when enabled, only commands whose first token matches
  this allowlist run; anything else is rejected before a process starts.

It shares the same throwaway-directory / stripped-env / hard-timeout /
output-cap execution model as `run_python_code`.

## Error categories

Every tool returns `{"success": false, "error": "...", "error_category":
"..."}` on failure, where `error_category` is one of `validation_error`
(empty/oversized input, bad timeout), `timeout_error` (execution killed
after exceeding its timeout — `details` carries whatever partial
stdout/stderr was captured before the kill), `unsupported_operation`
(`run_shell_command` disabled, or its executable isn't allowlisted), or
`execution_error` (anything unexpected — messages are written to be safe to
show an agent, no stack traces or host paths).

## Logging

Structured logs go to `logs/code_server.log` (same convention as the other
MCP servers): a short request id, tool name, elapsed time, and exit code on
success, or the error category on failure. Submitted code/command text and
full output are not logged — only what's needed to trace a call.

## Architectural decisions worth calling out

- **`python -I` (isolated mode), not a bare interpreter invocation.**
  Ignores `PYTHONPATH`/`PYTHONHOME` and the user site directory, so the
  already-stripped environment can't be partially defeated by leftover host
  interpreter configuration.
- **`subprocess.Popen` + `taskkill /F /T` on Windows, not
  `subprocess.run(..., timeout=...)` alone.** Windows doesn't give
  `subprocess.run`'s timeout a way to kill a process's children; a script
  that spawns a subprocess of its own would otherwise survive its parent's
  timeout kill.
- **A fresh temp directory per call, not a shared scratch directory.**
  Concurrent calls (this is an HTTP server; nothing serializes requests)
  must not see each other's files, and nothing should accumulate on disk
  across calls by default.
- **Env allowlist, not an env denylist.** A denylist has to be kept in sync
  with every secret this backend ever adds to `.env`; an allowlist of the
  handful of variables a subprocess actually needs to run (`PATH`, etc.) is
  safe by construction as the backend's configuration grows.
