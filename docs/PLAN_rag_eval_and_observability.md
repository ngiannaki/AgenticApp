# Plan: evaluation harness, hybrid search tuning, contextual retrieval, observability

Status: proposed (nothing implemented). Date: 2026-10-08.

## What already exists (this changes the scope)

- **Hybrid search + reranker are already built.** `backend/mcp_servers/retrieval/langchain_pipeline.py` runs pgvector
  cosine + Postgres full-text, fuses them with weighted RRF (`fuse_rankings`), reranks with a cross-encoder
  (`cross-encoder/ms-marco-MiniLM-L-12-v2`), then expands neighbours. So item 1 is **tuning and gap-filling, driven by
  the eval harness**, not a build from scratch. (`backend/mcp_servers/RAG_V2_PIPELINE.md` still describes Chroma; it is stale.)
- **No token/cost tracking.** LLM calls happen at `streaming_agent.py:660` (chat) and `agents/base.py:163` (agents);
  neither stores usage. `app.usage_daily` only counts chat turns, uploads and agent tasks.
- **No evaluation of any kind.** `backend/tests` has unit/contract tests only.
- **No admin role** in auth. Needed for a team-wide cost view.

## Order and why

| # | Piece | Size | Depends on |
|---|---|---|---|
| 1 | Evaluation harness | L | – |
| 2 | Observability + cost tracking | L | – (can run in parallel with 1; the eval reports cost/latency through it) |
| 3 | Hybrid search tuning | M | 1 (baseline first, then every change is measured) |
| 4 | Contextual retrieval | M | 1 (A/B against the baseline), 2 (indexing cost is tracked) |

You listed eval, hybrid, contextual, observability. I'd swap observability ahead of hybrid/contextual: it is
independent of retrieval, and contextual retrieval adds one LLM call per chunk, which you want to see the cost of.

---

## 1. Evaluation harness  → `backend/eval/`

**Goal:** one command tells you whether a change made retrieval or answers better or worse.

**Layout**
- `backend/eval/corpus/` small fixture documents (non-private). Candidates: a public 10-K/balance sheet, one paper, one docx, one xlsx.
- `backend/eval/datasets/*.jsonl` golden questions: `{id, question, type, document, gold:[{page|substring}], reference_answer, answerable}`.
  Types: single-fact, table lookup, multi-chunk, cross-section, unanswerable.
- `backend/eval/metrics.py`, `runner.py`, `judge.py`, `generate.py`, `__main__.py` (`python -m backend.eval run|compare|generate`).
- `backend/eval/runs/<timestamp>_<label>.json` + a markdown summary.

**Three layers**
1. **Retrieval (no LLM, fast, cheap):** recall@k, hit rate, MRR, nDCG. Gold is a page or text span, **not a chunk_id**, so
   results stay comparable when chunking changes. Calls `LangchainRetrievalPipelineV2` directly.
2. **Generation (LLM judge):** faithfulness (claims supported by retrieved text), answer correctness vs reference,
   citation accuracy (cited chunk really supports the claim), and abstention on unanswerable questions.
   Judge model set by `EVAL_JUDGE_MODEL`, separate from the chat model.
3. **System:** latency per stage, tokens, cost per question (from piece 2).

**Dataset creation:** `generate` samples chunks (including tables) and has an LLM draft question + reference answer + gold span;
a human reviews and edits the JSONL. Target ~50–100 questions to start. Synthetic-only sets flatter the system, so the review step is required.

**Experiments:** the runner accepts setting overrides (`--set dense_weight=0.7 --set use_reranker=false`) and sweeps, so each tuning idea is one run.
`compare` prints deltas against a baseline run and exits non-zero past a regression threshold.

**Isolation:** reuses the `TEST_DATABASE_ADMIN_URL` pattern in `conftest.py` to index the corpus into a throwaway database.

**CI honesty:** CI has no embedding model, so CI runs only unit tests for metrics, the runner and the judge parsing (stubbed).
The real eval runs locally or nightly; a `.github` job can be added later once a model is reachable.

**Acceptance:** baseline run committed; `compare` flags a deliberately broken config (e.g. `top_k=1`).

## 2. Observability and cost tracking

**Decision:** homegrown Postgres tables with a thin span helper shaped like OpenTelemetry (so it can be exported later).
Alternative is self-hosted Langfuse (free, nicer UI) but it adds services and sits outside your per-user auth and quotas. Easy to revisit.

**Schema (migration `0006_observability.sql`)**
- `app.llm_calls`: ts, user_id, trace_id, conversation_id/task_id, feature (`chat`, `agent:<name>`, `embedding`, `indexing_context`, `eval`),
  model, input/cached/output/reasoning tokens, latency_ms, ttft_ms, cost_usd, estimated (bool), status, error.
- `app.trace_spans`: trace_id, span_id, parent_id, kind (`llm`/`tool`/`retrieval`/`embedding`), name, start, duration_ms, attrs jsonb, status.

**Capture points**
- Chat: `streaming_agent.py:660`; add `stream_options={"include_usage": true}` to the streaming call.
- Agents: `agents/base.py:163`. The agents service reports through the internal API (new `POST /internal/usage`, same internal token) rather than touching the DB itself.
- Tool calls: wrap the MCP call path (`core/mcp_http.py` / `agent_tools.py`) to record duration and success.
- Retrieval stages: the MCP server returns `timings` (embed, dense, keyword, fuse, rerank, expand) in the result; the API stores them on the span.
- Embeddings: LangChain hides usage, so these are estimated and flagged `estimated=true`.
- Ollama (dev) may omit usage; fall back to an estimate, flagged, cost 0.

**Pricing:** `backend/config/pricing.py` ($/1M input, cached input, output per model). Local models cost 0; unknown model → cost null and flagged.
Tokens are stored, so history can be re-priced.

**Privacy:** metadata only by default. Prompts and answers are **not** stored (private documents, multi-user app).
Optional `OBS_STORE_PAYLOADS=true` for debugging, truncated. Retention job deletes spans older than `OBS_RETENTION_DAYS`.

**Reliability:** writes are batched and async; a logging failure never fails a request.

**Surfaces**
- `GET /api/usage/me` (today / 7d / 30d, by feature and model); `GET /api/usage/admin` behind a new `ADMIN_EMAILS` setting.
- Frontend page `/usage`: cost and tokens over time, by feature, slowest tools, error rate, top conversations.
- Chat: per-answer tokens/cost/latency in the Activity box; stage timings on retrieval rows.
- Optional later: a daily USD budget next to the existing quotas.

**Acceptance:** a chat turn and an agent task each produce linked spans and an `llm_calls` row; the numbers match the provider's usage; a forced tool error shows up as an error span.

## 3. Hybrid search tuning (after the baseline)

Known gaps in the current pipeline, each tested as an eval experiment and kept only if it wins:
1. **RRF weights** are a fixed 0.5/0.5 → sweep.
2. **Keyword leg** uses the `'simple'` config (no stemming) with an OR query ranked by `ts_rank_cd`, which is not BM25 → try an added `english` tsvector, or `websearch` for phrases.
3. **Reranker** is an English-only MiniLM model run in-process on CPU → compare `bge-reranker-v2-m3` (multilingual) and a hosted reranker API; measure the latency cost.
4. **No relevance cutoff:** a poor match still gets returned → add a rerank-score threshold so the agent can say "not found", measured by the unanswerable questions.
5. **Query rewriting:** multi-query / HyDE for vague questions (flag, off by default).
6. **Duplicate-heavy results** → MMR or near-duplicate removal before neighbour expansion.

Every knob is already a `RAG_V2_` setting or a small addition to `LangchainRagSettings`; defaults change only with eval evidence.

## 4. Contextual retrieval

**What:** at index time an LLM writes a 1–2 sentence context for each chunk ("This is from the FY2024 balance sheet, section Liabilities…").
That context is prepended to the text used for **embedding and keyword search**; the displayed chunk stays unchanged.

**Changes**
- Migration `0007`: `rag.chunks.context text`; rebuild generated `tsv` from `coalesce(context,'') || ' ' || content`; `rag.documents.contextualized bool`.
- `indexing/langchain_pipeline.py`: new step between split and embed. Settings: `RAG_V2_CONTEXTUAL_RETRIEVAL` (default **false**),
  `RAG_V2_CONTEXT_MODEL` (small, cheap model), `RAG_V2_CONTEXT_CONCURRENCY`.
- Prompt gets the section or surrounding window, not the whole document, so long documents stay affordable; the document-first prefix keeps provider prompt caching effective.
- A failure on one chunk falls back to no context for that chunk; it never fails the index.
- `scripts/reindex_all.py` gets `--contextual` to rebuild existing documents for A/B runs.
- Cost: one call per chunk, recorded under `feature=indexing_context` so the cost is visible per document.

**Acceptance:** eval A/B (contextual vs baseline) on the same corpus; keep the default off unless recall@k improves enough to justify the indexing cost.
Dev note: on local Ollama this makes indexing much slower, which is why it is opt-in.

---

## Decisions needed (my defaults in bold)

1. Corpus for the golden set: **public documents committed in `backend/eval/corpus/`**, or your own files kept out of git (`Profile.pdf`, `paper1_prod.pdf` are in the repo root).
2. Judge model: **a stronger model than the chat model, configured separately**.
3. Admin concept for the team cost view: **`ADMIN_EMAILS` env list**.
4. Observability store: **Postgres tables**, or self-hosted Langfuse.
5. Hosted reranker API acceptable, or keep everything local? **Local first**.

## Risks

- Synthetic questions overstate quality; mitigate with human review and including unanswerable/hard types.
- Judge variance; mitigate with a fixed judge, temperature 0, and reporting retrieval metrics (no judge) alongside.
- Contextual retrieval cost on large uploads; mitigate with opt-in, a cheap model and a per-document cap.
- Span volume; mitigate with batching, retention, and no payload storage by default.

---

## Scope update (2026-10-08)

Decided after review: this is a sample app, so the **evaluation harness (1) and hybrid tuning (3) are not going to be built** (token and cost burn
outweighs the value), and **per-chunk contextual retrieval (4) is dropped** (about one LLM call per chunk). Remaining:

- **Observability and cost tracking (2)**: approved as planned.
- **Zero-LLM context enrichment** (replaces 4): improve the context the chunker already attaches, using the TOC and document structure. Findings:
  1. Bug: heading state is per page, so a page with no heading of its own gets no `Section:` prefix and no h1/h2/h3 metadata
     (verified: page 2 prose after a `# Results > ## Revenue` page comes back with empty h1/h2). Fix: carry the heading state across pages.
  2. Document title is not part of the embedded text; matters when searching several documents at once.
  3. csv/xlsx/pptx chunks get no context prefix (sheet name, slide title); only pdf/docx/txt do.
  4. Optional: embed the TOC headings and boost chunks of the best-matching sections at query time.
