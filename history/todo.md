# Todo

Each entry is a `##` block dated `YYYY-MM-DD`, followed by `- [status-icon] task` lines (✅ done / ⬜ pending) so the log can be parsed into a table (date, status, task). Append new entries at the bottom; never edit or delete past entries — if a task from an earlier date gets done, add a new line under today's date referencing it rather than editing the old one.

## 2026-09-22
- ✅ Improve `retrieve_document_v2` tool description + agent nudge note so the model reliably uses `filters={"chunk_content_type": "table"}` when the user asks for tables specifically
- ⬜ Decide whether "tables only" should also be a deterministic UI toggle (bypassing LLM discretion) vs. relying on agent prompting alone
- ⬜ Decide whether "tables only" should support cross-document browsing, or stay scoped to a single document_id as today
- ⬜ Consider a dedicated "list table chunks" tool/endpoint that returns all `chunk_content_type=table` chunks for a document without requiring a semantic query (today's filter still runs through similarity search + rerank, so a table chunk far from the query text may not surface)

## 2026-09-22
- ✅ Add deterministic per-turn citations (server-built from actual retrieve_document_v2 results, not model-reported) with `[n]` markers in the model's prose + a clickable "Sources" strip in the chat UI
- ✅ Add `retrieval_get_document_chunks_v2` (unscored, full chunk listing) + `GET /api/documents/{id}/chunks`, kept out of the chat agent's own tool list
- ✅ Add a Documents-page "Preview chunks" panel showing every chunk with its tags (chunk_id, chunk_index, page/sheet/row, chunk_content_type, section)
- ⬜ pptx chunks still get no `chunk_content_type`/table tagging (existing gap, noted 2026-09-22) — citations and the chunk-preview panel will show pptx chunks with thinner tags (slide number only) than pdf/docx/csv/xlsx until that's addressed
- ⬜ Citation PDF preview only deep-links by page (`#page=N`) — no rendered page image/highlight of the exact chunk region within the page; consider if that's worth the added complexity
- ⬜ Verify in the actual chat UI: bracket numbering in the model's prose vs. the server-built citation list actually lines up in practice, not just in the prompt instruction

## 2026-09-24
- ✅ Add scanned-PDF detection (`_pdf_has_text_layer`) + OCR fallback (Docling `use_ocr=True` singleton) to `pdf_to_documents` so scans no longer silently index as near-empty
- ✅ Add `.xls`/`.ods` support to v2 indexing (detect.py extension map, xlsx_reader.py pandas `xlrd`/`odf` engines, loader skip of UnstructuredExcelLoader for legacy extensions)
- ⬜ Verify the OCR fallback end-to-end against a real scanned PDF (only tested that a normal text PDF is correctly detected as having a text layer — no scanned sample was available in-repo to confirm the OCR path itself produces usable markdown)
- ⬜ Verify `.xls` parsing end-to-end (only `.ods` was round-tripped with a generated file — no `xlwt`/legit `.xls` writer available to generate a test file; `.xls` path shares the same `_parse_legacy` code but engine="xlrd" specifically is unverified)
- ⬜ Other file-reading skill ideas intentionally not ported (JSON/JSONL support, ZIP/TAR archive ingestion, image OCR) — would be new content types, not fixes; revisit only if users actually need to upload those formats

## 2026-09-24
- ✅ Research Agent Skills format best practices (anthropics/skills repo + agentskills.io open spec) before designing our own
- ✅ Add feature-flagged `list_skills`/`get_skill` local tools to `AgentRuntime` (`API_SKILLS_ENABLED`, default off) with progressive disclosure (index always cheap, body loaded on demand)
- ✅ Migrate `backend/skills/SKILL.md` into the required `<name>/SKILL.md` folder convention (`backend/skills/file-reading/SKILL.md`)
- ⬜ Decide what to do with the migrated `file-reading` skill: it's written for claude.ai's sandbox (`/mnt/user-data/uploads/`, shell tools like `pdfinfo`/`extract-text`) which this agent doesn't have — rewrite around this project's actual `_v2` indexing/retrieval tools, or remove it
- ⬜ Decide what project-specific skills are actually worth authoring (e.g. "how to answer table-only questions using `chunk_content_type=table` filters", "how to cite sources using the grounding block", RAG-pipeline-specific know-how) — nothing written yet beyond the migrated file
- ⬜ Once real skills exist, flip `API_SKILLS_ENABLED=true` in `backend/.env` and verify in the live chat UI that the model actually calls `list_skills` unprompted when a task matches one
- ⬜ Consider adding a short system-prompt nudge (like the existing `_with_attached_documents_note` pattern) pointing the model at `list_skills` for skill-shaped tasks, if it doesn't reach for the tool reliably on tool-description alone

## 2026-09-24
- ✅ Write `sql-database-safety`, `rag-retrieval-strategy`, `citations-and-grounding`, `code-execution-and-artifacts` skills, grounded in actual tool behavior
- ✅ Fix `db_query_data` MCP tool signature dropping its `parameters` argument (docstring promised `%s` binding that silently never worked — values had no bound-parameter path)
- ✅ Decide what to do with the migrated `file-reading` skill — removed it, replaced with project-specific `document-ingestion` skill
- ⬜ Flip `API_SKILLS_ENABLED=true` in `backend/.env` and verify in the live chat UI that the model reaches for `list_skills`/`get_skill` unprompted on a table-filter question, a db-write request, a "generate a report" request, and a "why didn't my upload work" question — one real test per skill (now 5)
- ⬜ `db_query_data`'s `parameters` fix is code-only, not test-covered — no existing test suite exercises the db MCP server found during this session; verify manually against a real Postgres instance before relying on it in production

## 2026-09-25
- ✅ Write `spreadsheet-data-strategy` skill covering sheet discovery without a TOC, honest reporting when a question needs exhaustive/aggregate row coverage, and the sheet/row citation gap
- ⬜ Fix the citation gap: xlsx grounding `location` priority (`page → slide → sheet → row` in backend/mcp_servers/retrieval/langchain_pipeline.py `_grounding`) drops `row` whenever `sheet` is present — should surface both (e.g. `Sheet1!row 42`)
- ⬜ Decide whether to re-enable the disabled direct-read MCP tool (`indexing_read_document`, `@mcp.tool` commented out in backend/mcp_servers/indexing/server.py:442-443) for small/single-sheet files that don't need indexing at all
- ⬜ Design and build the discussed xlsx profiling pass (openpyxl read-only: sheet names, dimensions, header row per sheet) as a cheap always-run step, used both as a real TOC for spreadsheets and as the size signal for a direct-read-vs-index routing decision — currently neither exists
- ⬜ Consider a structured query tool (pandas/DuckDB over a sheet) for aggregate/exhaustive spreadsheet questions ("sum this column", "every row where X") that similarity search structurally cannot answer completely — flagged as a gap in the new skill rather than worked around
- ⬜ If a long sheet is indexed, consider batching rows per chunk (e.g. 20-50 rows with headers repeated) instead of today's one-row-per-chunk, to reduce chunk count for very long sheets — current one-row chunking was left as-is pending a decision on the above
- ⬜ Consider whether `sql-database-safety` and `code-execution-and-artifacts` count as "policies" vs. "skills" per the original personal_notes.md framing (skills/policies/concept/directions) — currently everything lives under one `backend/skills/` mechanism; revisit only if that distinction turns out to matter in practice

## 2026-09-25
- ✅ Build the structured query tool for spreadsheets flagged above — `code_profile_spreadsheet` + `code_run_python_code_on_document` (backend/mcp_servers/code/execution/spreadsheet.py), scoped to csv/xlsx/xlsm attached via the chat inbox (not Documents-page uploads, which still index as before)
- ✅ Route chat-inbox spreadsheet uploads (.csv/.xlsx/.xlsm) around indexing entirely (files.py `upload_file` branch); Documents-page uploads (`upload_document`) unchanged, always indexed
- ✅ Update `_with_attached_documents_note` to label attached files `(indexed)` vs `(NOT indexed)` and point the model at the right tool family for each
- ✅ Rewrite `spreadsheet-data-strategy` skill to lead with "which of two paths applies" before its existing indexed-path-only guidance
- ⬜ `.xls`/`.ods` aren't accepted by either upload endpoint at all (`_SUPPORTED_UPLOAD_EXTENSIONS` in files.py has no entry for them, 415-rejected) even though the indexing pipeline supports them elsewhere — the new direct-analysis path only covers csv/xlsx/xlsm today; decide whether to add them to both endpoints or leave as-is
- ⬜ No live end-to-end test yet — smoke-tested `profile_spreadsheet`/`run_python_code_on_document` directly against generated csv/xlsx files, bypassing the MCP/HTTP/chat-UI layers entirely; verify in the real chat UI that an attached xlsx actually skips indexing, the note labels it correctly, and the model reaches for the new tools unprompted
- ⬜ The xlsx profiling pass / TOC / direct-read-vs-index routing items above (2026-09-25, earlier block) are now effectively superseded for the chat-inbox path by this change — re-scope or close them; they still apply as-is to the Documents-page path, which is unchanged

## 2026-09-24
- ✅ Rewrite all five skills from tool-reference docs into procedures (numbered steps, coverage/verification checkpoints, retry budgets, stop conditions, anti-pattern tables, worked examples)
- ✅ Make the Sources strip show only chunks the answer actually cites (`_cited_only` in streaming_agent.py), so the new retrieval-retry loop doesn't flood it with unused chunks
- ⬜ `_cited_only` has a known minor imprecision: a literal like `arr[1]` in an answer's code block counts as citing source 1. Only affects which sources are listed (never corrupts answer text) — revisit by skipping fenced code spans if it shows up in practice
- ⬜ Verify the retrieval procedure actually changes behavior on `gpt-oss:20b` — specifically whether it performs the DIRECT/PARTIAL/ABSENT coverage check and retries with a reworded query, or just does one retrieval as before. If it doesn't follow multi-step procedures, consider trimming to the 3-4 highest-value rules per skill
- ⬜ Measure whether skills cost more iterations per turn (list_skills + get_skill = 2 extra tool calls against the 15-iteration ceiling) — may need max_iterations raised when skills are enabled
- ⬜ Consider a `references/` tier (spec's progressive-disclosure tier 3) if any skill outgrows ~5,000 tokens; `get_skill` currently reads SKILL.md only and would need a file argument to support it
- ⬜ No automated tests cover the skills loader or `_cited_only` beyond manual verification this session — worth a small pytest file if this becomes load-bearing

## 2026-09-24
- ✅ Diagnose why skills were never used in live testing — skill discovery was gated behind a `list_skills` tool the model never called
- ✅ Move the skill index into the session system prompt (`AgentRuntime.system_prompt_for`), wired into both session creation and Mongo rehydration
- ✅ Remove the now-redundant `list_skills` tool, keeping `get_skill` as the on-demand body fetch
- ⬜ Re-test with `API_SKILLS_ENABLED=true` **in a brand-new conversation** (system prompt is fixed at session creation — an existing chat won't see the change) and confirm `get_skill` is actually called
- ⬜ If `get_skill` now gets called but the *procedure* still isn't followed (one blind retrieval instead of the decompose/coverage-check/retry loop), that's a separate problem from discovery — the fix would be trimming each skill to its 3-4 highest-value rules rather than a full procedure, since gpt-oss:20b may not sustain a 7-step loop
- ⬜ Watch for the opposite failure now that skills are advertised in every prompt: calling `get_skill` on every turn regardless of relevance, burning an iteration. The "don't call it at all when no skill matches" instruction is untested
- ⬜ Consider re-adding a `list_skills`-style tool only if the skill count grows enough that the always-on index becomes expensive (~630 tokens for 5; would matter around 20+)

## 2026-09-24
- ✅ Fix PROD tool calls failing on a reasoning-capable model (`gpt-6-luna`/`gpt-5.6-sol`) by adding `OPENAI_REASONING_EFFORT`, paired with `_PROD` model vars, generalized `get_ollama_extra_body`→`get_chat_extra_body`
- ⬜ `OPENAI_REASONING_EFFORT` is a single global setting shared by every `_PROD` model (chat, retrieval, retrieval_agent, metadata) — if you ever mix a reasoning model for one functionality and a classic model for another simultaneously, this breaks (one call needs the param, the other must not have it). Not an issue today since all `_PROD` vars currently point at the same model family, but worth remembering if that changes
- ⬜ Verify retrieval/metadata/retrieval_agent PROD calls (not just the main chat agent) also succeed now, since they share the same `get_chat_extra_body()` — only the main chat path was actually exercised by the reported error
- ⬜ Confirm whether `gpt-5.6-sol` actually needs `reasoning_effort="none"` specifically, or would work with `"low"`/`"medium"` too now that tool calls are unblocked — "none" was chosen only to match the error message's suggested fix

## 2026-09-24
- ✅ Reconcile DEFAULT_SYSTEM_PROMPT's "don't call tools speculatively" rule with the skills block's "call get_skill first" instruction — added an explicit override sentence to `system_prompt_for`'s skills block
- ⬜ Re-test with a brand-new conversation (system prompt fixed at session creation) and confirm `get_skill` now actually gets called — this was a real candidate explanation for the earlier "skills never used" finding, but not yet confirmed as *the* fix versus just *a* fix

## 2026-09-24
- ✅ Add two universal rules to DEFAULT_SYSTEM_PROMPT: verify a tool result before reporting success, don't present an ungrounded fact as tool-derived
- ⬜ Watch for over-caution from the new "verify before reporting success" rule — in principle it could make the model hedge on results that were actually fine (e.g. re-checking a trivially obvious success). Not observed yet, just a plausible side effect of a stronger verification instruction worth watching for in real use

## 2026-09-24
- ✅ Fix a live citation-mismatch bug (correct "page 51" claim, Sources chip pointed at an unrelated page 40 decoy table) by replacing `[n]` bracket-number citations with a `<citation id="..."/>` tag the model copies verbatim from the chunk it's citing
- ⬜ Re-test in a brand-new conversation against the exact NVIDIA document/query that surfaced this bug, and confirm the Sources chip now correctly points at page 51
- ⬜ Watch whether the model reliably copies the `cite_as` tag byte-for-byte (long opaque chunk_id strings are more error-prone to reproduce than a short number was) — a copy mistake now fails safe (dropped citation) rather than silently wrong, but dropped citations are still a real quality loss if they happen often; would show up as answers with fewer Sources chips than expected
- ⬜ No test coverage yet for `_collect_and_stamp_citations`/`_render_citations` beyond this session's manual verification — worth a small pytest file given this is now the second citation bug found in production

## 2026-09-24
- ✅ Fix citation display numbers showing stamp-order gaps (e.g. `[4]`/`[7]`) instead of sequential `[1]`/`[2]` — renumber locally in `_render_citations` by order of appearance in the final answer
- ✅ Root-cause the excessive retrieval back-and-forth (7 calls, guessed pages 49→50→51) to `get_table_of_contents_v2` being silently truncated at 8000 chars for a 251-heading document (real size 23,596 chars) — the model never saw the page-51 entry it needed. Raised `max_tool_result_chars` default to 32,000
- ⬜ Re-test the exact NVIDIA query again in a fresh conversation and confirm: (a) Sources chips now read `[1]`/`[2]`, (b) the model goes close to directly to `filters={"page": 51}` on its first or second retrieval call instead of guessing 49→50→51 — this second part is the one part of today's fix that's inference from root-causing the log, not yet observed working
- ⬜ 32,000 chars covers this one document's TOC; a document with a substantially deeper heading structure (this was only h1-level, 251 entries) could still exceed it — if truncation recurs, check the actual payload size before raising the cap again, don't just guess bigger
- ⬜ Consider whether `get_table_of_contents_v2` should have its own, more generous cap independent of every other tool's result (a TOC is uniquely likely to be huge-but-legitimate), rather than sharing one global `max_tool_result_chars` — not done today since a global raise already covers the observed case

## 2026-09-24
- ✅ Add generate_ui direction for charts/dashboards: self-contained inline SVG + vanilla JS instead of an external charting library (Plotly/Chart.js), which silently fails to render in the sandboxed iframe (storage access throws, halting all script execution) — updated both the tool's own docstring and the code-execution-and-artifacts skill
- ⬜ Verify in the live chat UI that a future dashboard/chart request actually produces SVG-based output rather than reaching for Plotly again — not yet observed with the updated guidance in place
- ⬜ The storage-throw explanation is grounded in the sandbox attributes and a well-documented browser behavior, not something directly reproduced/logged this session (no way to see the sandboxed iframe's console from here) — worth confirming in devtools next time this comes up, in case there's an additional or different contributing cause

## 2026-09-24
- ✅ Fix theme/debug-mode UI preferences resetting when navigating between chat (`/`) and Documents (`/documents`) — centralized into `frontend/src/lib/preferences.ts` (localStorage) + shared `useTheme()` hook, plus a no-flash theme-init script in `layout.tsx`
- ✅ Add a theme toggle to the Documents page (previously had none, so it always rendered in the default/light look)
- ⬜ `debugMode` is only meaningful on the chat page today — Documents page doesn't read/use it; revisit only if a debug-style view is added there too
- ⬜ No automated/browser test covers the cross-page persistence fix beyond manual `tsc`/`eslint`/200-status checks this session — worth a quick manual click-through (toggle dark on Documents, go to chat, confirm it's still dark and vice versa) next time the dev server is up

## 2026-09-24
- ✅ Fix the actual root cause of preferences resetting on navigation: `useTheme`'s effect-based hydration was writing the wrong default (`"light"`) back into `localStorage` on every mount, racing the correction effect — switched to synchronous lazy `useState` init + write-only-on-explicit-setter-call
- ✅ Apply the same lazy-init fix to `debugMode` in ChatApp.tsx (had the reset-then-correct flash, though not the storage-corruption bug)
- ✅ Add a `mounted` gate to the Documents page's theme toggle icon to avoid a hydration mismatch now that its theme value is resolved synchronously on the client (ChatApp doesn't need this — it renders `null` until auth resolves, so nothing paints during static prerendering)
- ⬜ Still not verified by an actual manual click-through in a browser this session — only `tsc`/`eslint`/200-status checks; do the toggle-dark-on-Documents-then-check-chat walkthrough next time the dev server is up

## 2026-09-24
- ✅ Add `web_search`/`fetch_page` tools to the retrieval MCP server (DuckDuckGo via `ddgs`, page extraction via `trafilatura` — no paid API key, per user preference) instead of standing up a new MCP server
- ✅ Add SSRF guard to `fetch_page` (rejects non-http(s) schemes and hostnames resolving to loopback/private/link-local/reserved addresses) before any request is made
- ✅ Add `web-search` skill (`backend/skills/web-search/SKILL.md`) covering when to use web search vs. `retrieve_document_v2`, snippet-vs-fetch judgment, and prose attribution (no `cite_as` tag exists for web results)
- ✅ Verify `web_search` live (real DuckDuckGo query returned results) and `fetch_page`'s SSRF guard (blocked `169.254.169.254` metadata address and `file://` scheme)
- ⬜ Could not verify `fetch_page`'s actual HTTP fetch path end-to-end in this sandbox — outbound HTTPS here fails with `CERTIFICATE_VERIFY_FAILED` even for plain `httpx.get`, confirmed environment-wide (not code-specific); re-verify against a real page once run outside this sandbox
- ⬜ `ddgs`/`trafilatura` added only to `backend/mcp_servers/retrieval/requirements.txt` — confirm the deployed retrieval-server environment actually installs from that file (not just the shared venv used during this session)
- ⬜ No automated test coverage for `web_search`/`fetch_page` beyond this session's manual/live checks — worth a small pytest file (mocking `ddgs`/`httpx`) given every other MCP tool in this repo has none either, but this one has a security-relevant SSRF guard worth pinning down with tests
- ⬜ DuckDuckGo has no SLA/API key — expect occasional rate-limiting or backend errors in production; `web-search` skill tells the model not to retry more than once, but this hasn't been observed under real rate-limiting yet

## 2026-09-30
- ✅ Add `MCP_TRANSPORT=stdio` toggle to all 4 MCP servers' `__main__` blocks (code/db/indexing/retrieval), so they can run as subprocesses instead of only standalone HTTP services
- ✅ Add `backend/apps/client/servers.docker.json` (stdio variant of `servers.json`, selected via existing `API_SERVERS_PATH` setting) — `servers.json` itself left untouched for `start_mcp_servers.ps1`
- ✅ Add `backend/requirements-base.txt` — pinned snapshot of the previously-undocumented shared baseline deps (fastapi/fastmcp/mcp/pydantic-settings/motor/pymongo/llama-index-core/chromadb/openai/etc.), captured via `pip freeze` against the real working venv since `backend/pyproject.toml` is empty
- ✅ Add root `Dockerfile` (single container: API + 4 MCP servers as stdio subprocesses) and `.dockerignore` (excludes `backend/.env` and other secrets)
- ✅ Add root `docker-compose.yml` (app + Mongo + Postgres + Chroma, one `docker compose up --build`), reusing `backend/.env` via `env_file:` with hostname overrides for the compose network
- ✅ Add `RUNNING.md` documenting both ways to run (`start_mcp_servers.ps1` vs. `docker compose up --build`) side by side
- ✅ Verify all 4 edited `server.py` files compile (`py_compile`) and `docker compose config` validates the compose file + `.env` merge correctly
- ✅ Ran `docker build`/`docker compose up` end-to-end (previously untested) — found and fixed a missing dependency (`llama-index-vector-stores-chroma`) and an env-stripping bug (stdio subprocess env overrides weren't reaching the `db` MCP server, see history.md 2026-09-30); confirmed clean startup, 25 tools connected, `/api/health` returns 200, no errors in logs
- ✅ Fixed a real document upload failing in Docker: missing X11 libs for docling's OpenCV dependency (`libxcb.so.1` and friends), missing `PyPDF2` (also missing outside Docker — pre-existing gap in the docling-failure fallback path), and an OpenAI embeddings batch-size bug (`chunk_size` defaulting to 1000 texts/request, exceeding the 300k-tokens-per-request cap on large documents) — verified end-to-end against a real small PDF through the actual `LangchainIndexPipelineV2.index_document` call, not just import checks
- ⬜ The original NVIDIA 10-K upload that surfaced these bugs was never re-tried after the fixes (tested against a smaller PDF instead, at the user's request, to keep iteration fast) — worth re-uploading that specific large file once to confirm the `chunk_size=100` fix holds at that document's actual scale (653k tokens), not just on a small file
- ⬜ `chunk_size=100` for `OpenAIEmbeddings` was chosen conservatively, not tuned — smaller batches means more HTTP round-trips to OpenAI, so indexing a very large document will be slower (more requests) though it now works instead of failing outright; revisit if indexing latency on large docs becomes a real complaint
- ⬜ Rebuilding the Docker image after any dependency change currently reinstalls the entire pip layer from scratch (~9 minutes, since all `pip install` calls for every component are chained in one `RUN`) — fine for now, but worth splitting into separate cached layers per component if the dependency set keeps changing during active development
- ⬜ `AUTH_DEV_BYPASS=true` is still on in `backend/.env` — must be set to `false` before this is deployed anywhere reachable by more than one person (every request is currently treated as one hardcoded dev user)
- ⬜ Pick and set up free-tier hosting: backend (Render/Fly.io/Railway free web service, needs the new Dockerfile), frontend (Vercel, Next.js-native), managed free Mongo (Atlas) and Postgres (Neon/Supabase) instead of the compose file's local containers — Chroma has no obvious managed free tier, needs a persistent-disk host or a swap to pgvector on the same free Postgres
- ⬜ Bring `frontend/` into the main repo properly — it currently has its own nested `.git`, so it's untracked/invisible to the outer repo's history and can't be deployed via CI as-is
- ⬜ No CI (`.github/workflows` doesn't exist) and no organized test suite (only ad hoc scripts under `backend/mcp_servers/client/`) — worth adding before treating any of this as "production"
- ⬜ Resolve the three parallel streaming-agent implementations (`streaming_agent.py`/`v2`/`v3`) — `main.py` currently has a "ROLLBACK" comment noting v2/v3 were experimental; pick one before calling the backend done
- ⬜ Root has no single `README.md` (just scattered `QUICK_START.md`/`HOW_TO_RUN_SERVERS.md`/etc. plus the now-more-canonical `RUNNING.md`) — consider consolidating into one entry point
