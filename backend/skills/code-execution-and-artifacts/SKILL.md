---
name: code-execution-and-artifacts
description: The procedure for producing something by running code or generating a file — choosing between code_run_python_code, code_generate_file and code_generate_ui, building interactive charts/dashboards that actually render inside generate_ui's sandbox (self-contained SVG, not an external charting library), verifying the run actually produced what you expected (not just exit code 0), fixing failures within a retry budget, and surfacing plots/documents/spreadsheets back to the user. Use this whenever a request needs computation, a chart, a dashboard, a generated document or spreadsheet, or an interactive component.
compatibility: Requires the code MCP server (backend/mcp_servers/code). code_generate_ui is only present when the operator has enabled it; run_shell_command is off by default, so nothing can be pip-installed.
---

# Code execution procedure

## Step 1 — Pick the tool

| Goal | Tool | Why not the others |
|---|---|---|
| Compute a result, analyze data, draw a static chart | `code_run_python_code` | Only tool that actually executes |
| Produce a .pptx deck/.docx/.xlsx/.csv/.md/.txt from data you already have | `code_generate_file` | Renders real formatting declaratively — no code to get wrong |
| Something the user clicks/interacts with — an interactive chart, dashboard, form, calculator | `code_generate_ui` | Renders inline as a live sandboxed page |

If `code_generate_ui` is not in your tool list, the operator disabled it — answer without it (a static chart from `run_python_code`, or a table). Don't ask the user to enable it.

Do not hand-write pptx/docx/xlsx/csv formatting inside `run_python_code`. `generate_file` produces proper Word heading styles, bordered tables, styled Excel headers and Excel-safe CSV encoding with far fewer ways to fail.

## Step 2 — generate_ui: build charts as self-contained SVG, not an external library

`generate_ui`'s page runs in a browser iframe sandboxed with `allow-scripts allow-forms` and **deliberately no `allow-same-origin`** — that's the actual security boundary, not an accident, so it never gets loosened for a chart library. One direct consequence: `localStorage`/`sessionStorage`/`indexedDB` are unavailable and **throw** if touched.

This matters specifically for charting: Plotly, Chart.js, and most other charting libraries touch storage somewhere in their init path. That throw is an uncaught, synchronous error — it halts **all** script execution on the page before anything renders, with no visible console for the user to explain why the page is blank. This has been hit in practice: a Plotly-based dashboard rendered nothing, with no error surfaced anywhere.

**Default to self-contained inline SVG + vanilla JS for any chart or dashboard in `generate_ui`.** No CDN fetch, no storage access, nothing that can fail this way:

- **The chart itself**: `<svg>` with `<rect>`/`<circle>`/`<path>`/`<line>` elements you compute the coordinates for directly in a `<script>` block. For anything beyond a bar/line/scatter plot, this is genuinely less code than wiring up a charting library's API, config objects, and data-format conversion.
- **Hover details**: `onmouseover`/`onmouseout` (or `addEventListener("mouseenter"/"mouseleave")`) on each shape, toggling a tooltip `<div>`'s position/content/visibility.
- **Legend filtering**: a `<div>`/`<li>` per series with an `onclick` that toggles that series' SVG elements' `display` (or opacity), and dims the legend entry to show its state.
- **Year-selection / other controls**: a `<select>` or range `<input>`, redrawing (or toggling visibility of pre-rendered groups) `onchange`/`oninput`. Pre-computing all years' shapes upfront and toggling `display` is simpler and more reliable than recomputing SVG paths on every interaction.

Reach for an external CDN library only when the task specifically needs it (e.g. a physics or game engine with no reasonable from-scratch equivalent) — not for charts, where the self-contained approach is both more reliable and usually simpler.

If the user asks for something Plotly/Chart.js/etc. produced and it isn't rendering, don't debug the library integration — rebuild the same visualization in SVG. That's a rewrite, not a bug fix, and it's the faster path to something that actually works in this sandbox.

## Step 3 — Know run_python_code's sandbox constraints before you write code

These are not negotiable and cause most avoidable failures:

- **No network, no secrets, no database.** The subprocess runs with a stripped environment — this backend's API keys and DB URL are not present. Code cannot query Postgres. To chart or analyze database data: call `db_query_data` first, then embed the returned rows into the Python source as a literal. Same for document content: retrieve it, then pass it in.
- **No installing anything.** `run_shell_command` is disabled by default, so there is no `pip install`. The interpreter is this project's venv, so its libraries (pandas, numpy, matplotlib, openpyxl, …) are importable. If an import fails, rewrite using the stdlib or an available library — do **not** attempt to install it.
- **Timeout**: 10s by default, 60s maximum via `timeout_seconds`. Pass a higher value explicitly for heavy work rather than letting it get killed.
- **Output cap**: combined stdout/stderr is truncated past ~200,000 bytes. Print summaries, not full data dumps — `print(df.head())` and `print(df.shape)`, never `print(df)` on a large frame.
- **Artifacts**: at most 5 files per run, 10MB each, captured automatically from the working directory.
- **No display**: never call `plt.show()`. Save with `plt.savefig("name.png")` using a relative path.

## Step 4 — State your success criterion, then write the code

Before calling the tool, decide what the output must look like for the run to count as successful ("stdout should print a mean around 40–60", "artifacts should contain one .png"). You will check against this in Step 6; without it you have no way to tell a correct run from a run that merely exited cleanly.

Make the code print what you need to read. A script that computes silently and prints nothing gives you `success: true` and no information.

## Step 5 — Syntax-check non-trivial code first

`code_check_python_syntax(code)` is free (no subprocess) and returns the exact line/column of a syntax error. Use it for anything beyond a few lines. A pass means it parses — nothing more, so still execute it.

## Step 6 — Verify the result, not the exit code

`success: true` means only that the process exited 0. Before reporting anything to the user, check:

- [ ] `stdout` actually contains the values I expected (Step 4), not empty output.
- [ ] If I expected a file, `artifacts` is non-empty and contains it.
- [ ] `truncated` is false — if true, my output was cut off and I may be reading a partial result.
- [ ] `timed_out` is false.
- [ ] The numbers are *plausible* (no NaN where a value belongs, no empty dataframe, no zero-length result).

A clean exit with empty or nonsensical output is a failure. Treat it as one.

For `generate_ui`, there's no exit code to check at all — the tool call succeeding only means the HTML was stored, not that it renders. Mentally re-read what you wrote for anything that touches storage, `window.top`, or an external library per Step 2 before considering it done.

## Step 7 — Failure loop, with a budget

On `success: false`, read the actual traceback in `stderr` and fix that specific error. **Budget: 3 attempts.** Change something real each time — re-running identical code produces an identical failure.

After 3 failed attempts, stop and tell the user what actually broke, quoting the real error. Do not:
- Report success you didn't observe.
- Invent plausible-looking output you never saw.
- Silently drop the failing part and answer around it.

On `timed_out: true`, the process (and its children) were killed. Reduce the work or raise `timeout_seconds` toward the 60s ceiling — don't immediately resubmit the same code unchanged.

## Step 8 — Surface the output

- `run_python_code` → files come back in `artifacts` as `{filename, url, ...}`. **Reference the `url` in your reply** — `![chart](url)` for images, `[data.csv](url)` for files. A produced artifact you never link is invisible to the user, even though the run succeeded.
- `generate_file` → returns `{url, filename, ...}` directly. Same rule: link it.
- `generate_ui` → the chat UI auto-renders a live preview the moment the call completes. Do **not** paste the URL; just describe what you built in a sentence or two.

## generate_file content shapes

- **txt/md/docx**: `{"title": "...", "sections": [{"heading": "...", "level": 1-4, "paragraphs": [...], "bullets": [...], "numbered": [...], "table": {"headers": [...], "rows": [[...]]}}]}`
- **pptx**: the document shape plus top-level `subtitle`, `metrics: [{label, value, note, trend?}]` (KPI slides) and per-section `chart: {title, labels, series: [{name, values}], type?: "line"}` and `notes`. Title -> title slide, each section -> a slide (text + native chart), each `table` -> its own table slide. Keep slide text short (3-6 bullets).
- **csv**: `{"headers": [...], "rows": [[...]]}` (`headers` optional, `rows` required)
- **xlsx**: as csv for one sheet, or `{"sheets": [{"name": "Q1", "headers": [...], "rows": [[...]]}]}`

Every row must have exactly as many cells as `headers` (or as the first row when there are none). Sheet names truncate at 31 characters.

## Anti-patterns

| Don't | Why |
|---|---|
| Report success without reading stdout | Exit 0 ≠ correct result |
| Retry identical failing code | Same input, same failure, wasted iteration |
| `pip install` anything | Shell tool is disabled; the call just fails |
| Query the DB from inside Python | No credentials in the sandbox — use `db_query_data` and pass values in |
| `print()` a whole dataframe | Blows the 200KB output cap and truncates what you needed |
| Build a .docx or .pptx by hand in Python | `generate_file` does it properly with less risk |
| Produce an artifact and not link its URL | User never sees it |
| Load Plotly/Chart.js/etc. via CDN in `generate_ui` for a chart | Silently blank page — the library's storage access throws in the sandbox and halts all script execution, with no visible error |
| Debug a non-rendering charting-library integration | Rebuild in SVG instead — faster and actually works here |

## Worked example: static chart

> **User:** "Chart the top 5 customers by revenue from the orders table."

1. Data lives in Postgres; the sandbox can't reach it → `db_query_data("SELECT customer, SUM(total) AS revenue FROM orders GROUP BY customer ORDER BY revenue DESC LIMIT 5")`.
2. Success criterion: a PNG in `artifacts`, and stdout echoing the 5 rows.
3. `run_python_code` with those 5 rows embedded as a literal list, matplotlib, `plt.savefig("top_customers.png")`, plus a `print()` of the data.
4. Verify: `artifacts` has the png, stdout shows 5 rows, `truncated`/`timed_out` false.
5. Reply with a one-line summary and `![top customers](url)`.

## Worked example: interactive dashboard

> **User:** "Build an interactive dashboard of revenue by region, with a year selector."

1. `db_query_data(...)` for revenue by region and year.
2. `generate_ui`, not `run_python_code` — the user asked for something interactive.
3. Build inline SVG bars/lines (not a CDN library, per Step 2), with each year's shapes pre-rendered into a `<g data-year="2024">`-style group; a `<select>` toggles which group has `display: block` via a small vanilla-JS `onchange` handler. Hover tooltips via `onmouseover` on each bar showing the exact value.
4. No exit code to check — re-read the HTML for any storage access or external `<script src>` before calling it done.
5. Describe what was built in one or two sentences; the preview renders automatically.
