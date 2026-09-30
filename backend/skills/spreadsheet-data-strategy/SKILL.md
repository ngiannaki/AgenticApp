---
name: spreadsheet-data-strategy
description: How to work with a spreadsheet document (csv/xlsx/xlsm/xls/ods) — which of two completely different paths applies (indexed+RAG vs. direct pandas analysis, decided by where it was uploaded, not by asking), how to discover sheets/rows/columns on each path, handling "give me every row"/"sum this column"/"how many rows match X" questions, and the known citation gap for indexed spreadsheets where a chunk's sheet is shown but its row number is dropped. Use this whenever the question is about a spreadsheet document, not just any document.
compatibility: Two separate systems, as of 2026-09-25. (1) Documents-page uploads (routers/files.py upload_document) and any document_id typed directly in chat always go through backend/mcp_servers/indexing's v2 pipeline — row-per-chunk indexing, no sheet-level TOC, grounding location fallback order page → slide → sheet → row (backend/mcp_servers/retrieval/langchain_pipeline.py). (2) A csv/xlsx/xlsm attached via the chat inbox's own attach button (routers/files.py upload_file, session-scoped) skips indexing entirely for those extensions and is analyzed directly via the code MCP server's profile_spreadsheet/run_python_code_on_document (backend/mcp_servers/code/execution/spreadsheet.py) — real pandas against the real file, not retrieval. Sits on top of the general loop in rag-retrieval-strategy for path (1) only.
---

# Spreadsheet data strategy

## Which path applies — check this first, don't guess

Two completely different mechanisms exist for the *same file types*, and which one is live depends entirely on **how the file got into the conversation**, not on anything about the file itself:

| How it got here | What happened to it | How to work with it |
|---|---|---|
| Uploaded via the Documents page, or referenced by a bare `document_id` typed in chat | Indexed into the v2 vector store (row-per-chunk) | `retrieval_*_v2` tools — rest of this skill |
| Attached via the chat inbox's own attach button, **and** it's csv/xlsx/xlsm | NOT indexed — raw bytes only | `code_profile_spreadsheet(document_id)` then `code_run_python_code_on_document(document_id, code)` |

`_with_attached_documents_note` (streaming_agent.py) tells you which case you're in every turn — it labels attached files explicitly as `(indexed)` or `(NOT indexed)`. Trust that label; don't infer it from the file extension alone, since the same `.xlsx` behaves differently depending on upload surface. If a spreadsheet shows as `(NOT indexed)`, calling any `retrieval_*` tool on its `document_id` will simply find nothing — that's not a bug to route around, it's the label telling you which tool family to use instead.

## The direct-analysis path (chat-inbox csv/xlsx/xlsm) — use this for exact answers

This path exists specifically because retrieval can't guarantee completeness (see below) — so when it's available, prefer it for anything needing an exact count, sum, filter, or full listing, rather than reaching for retrieval-style workarounds.

1. **`code_profile_spreadsheet(document_id)`** first — sheet names, exact row/column counts, column headers, and 3 sample rows per sheet, with no code execution. This is the real, complete sheet list a spreadsheet otherwise has no TOC for (see below) — every sheet, not a sample.
2. **`code_run_python_code_on_document(document_id, code)`** for the actual question — write real pandas against `sys.argv[1]` (the file's path inside the sandbox; never hardcode a filename). This runs in the same throwaway-subprocess sandbox as `run_python_code`, just with the one file pre-copied in. `print()` whatever answers the question; that's what comes back as `stdout`.

Because this reads the entire file, "every row where X" and "sum this column" are answerable *exactly* here — no hedging needed, unlike the indexed path below. Cite the code and its printed result as your evidence; there's no `cite_as` tag mechanism for this path (nothing was retrieved), so just show the relevant pandas output directly.

## The indexed path (Documents page / bare document_id) — everything below this line

A spreadsheet indexed this way is chunked as **one chunk per row** (`document-ingestion` skill), each tagged `chunk_content_type="table"`. That shape breaks two assumptions the normal retrieval loop relies on: that a table of contents can orient you, and that a handful of top-`k` chunks can represent "the answer." Neither holds for a spreadsheet the way it holds for a report — and unlike the direct-analysis path above, there is no way to get an exact/complete answer here at all.

### There is no sheet-level TOC — reconstruct it if asked

`retrieval_get_table_of_contents_v2` returns an empty `toc` for csv/xlsx by design (no headings exist in a spreadsheet). If the user asks "what sheets does this have" or "what's in this workbook," don't report the TOC as evidence of anything — it's empty for every spreadsheet, not just this one.

Instead, run a broad retrieval (`top_k=25`, no query bias — something like `query="overview"` or the document's own name) and read the `sheet` field off the returned chunks' grounding. The distinct `sheet` values you see are a **sample of what exists, not a guaranteed-complete list** — with many sheets or many rows per sheet, a similarity search can easily miss an entire sheet whose content doesn't match your query. Say so if you're reporting a sheet list this way: "these are the sheets I found in a sample of the data — there may be others."

CSV files have no `sheet` field at all (single implicit sheet) — don't ask for one.

### "Every row where X" / "sum this column" / "how many rows total" — retrieval cannot promise completeness

This is the important one. `retrieval_retrieve_document_v2` is similarity search over row-chunks with a capped `top_k` (25 max). It was never built to guarantee exhaustive coverage, and there is currently **no structured query tool** (no pandas/SQL execution over the full sheet) to fall back on for aggregate or exhaustive questions.

Concretely:
- "List every row where status = pending" — a `top_k=25` search returns the 25 *most similar* row-chunks to your query, not all matching rows. If there are 30 pending rows, you will likely see only some of them, with no signal that any were cut.
- "What's the total revenue across all rows" — you cannot compute a trustworthy sum from a sampled subset of rows. Don't attempt the arithmetic and present it as the total.
- "How many rows are there" — `retrieval_get_document_info_v2`'s chunk count is a legitimate answer for a csv/xlsx (one chunk per row, so chunk count ≈ row count for a single-sheet file); for a multi-sheet workbook it's the sum across all sheets, not any one sheet's count.

When a question needs completeness and no complete-data tool exists, say that plainly instead of quietly answering from a partial sample:

> "I can find and cite individual matching rows, but I can't guarantee I've found *every* row matching that — retrieval over a spreadsheet returns the closest matches, not an exhaustive scan, and this system doesn't yet have a tool that queries the full sheet directly. Here's what I found: [...]"

This is the same honesty principle as `rag-retrieval-strategy`'s Step 6 stop condition, applied to a failure mode that's specific to tabular data: a confident-looking row list that is silently incomplete is worse than admitting the limit.

### The sheet/row citation gap

The grounding block's `location` picks one field by priority: `page → slide → sheet → row`. For xlsx chunks that carry both `sheet` and `row`, **`sheet` wins and `row` is dropped from what's shown** — a citation for an xlsx row currently displays only the sheet name, not the row number, even though the row number exists in the chunk's own metadata. CSV citations are unaffected (no `sheet` field to shadow `row`).

Practical effect: when citing a specific xlsx row, the visible source location will be coarser ("Sheet1") than for a csv row ("row 42"). This is a known pipeline gap, not something to work around by inventing a row number yourself — never state a row number that isn't in the chunk text or its `cite_as`-tagged grounding. If precision matters to the user's question, say the sheet name and quote enough of the row's own content (which does contain every column's value) to make the specific row identifiable.

### What not to do

| Don't | Why |
|---|---|
| Use `retrieval_*_v2` tools on a spreadsheet labeled `(NOT indexed)` in the attached-files note | Nothing was indexed for it — you'll get an empty/error result, not a real "no data" answer; use `code_profile_spreadsheet`/`code_run_python_code_on_document` instead |
| Use `code_profile_spreadsheet`/`code_run_python_code_on_document` on a spreadsheet labeled `(indexed)`, or on a bare `document_id` the user typed | Those were only ever indexed, not stored for direct analysis — the code tools will find no file for that `document_id` |
| Report an empty TOC as "this spreadsheet has no data" | TOC is always empty for csv/xlsx by design on the indexed path — it says nothing about content |
| Treat a `top_k` sample as "all rows matching X" (indexed path) | Similarity search is not an exhaustive scan; nothing enforces completeness. On the direct-analysis path, use real pandas filtering instead — that *is* exhaustive |
| Sum/average/count values across retrieved chunks and present it as the sheet's true total (indexed path) | You're aggregating a sample, not the data. On the direct-analysis path, compute it for real in `code_run_python_code_on_document` instead of hedging |
| State a row number for an xlsx citation that isn't actually in the chunk metadata/text (indexed path) | The sheet/row gap above means it may not be there — don't fill the gap with a guess |
| Assume a reconstructed sheet list (from sampled chunks) is complete (indexed path) | Say it's a sample if you built it from retrieval results rather than a real listing — or, on the direct-analysis path, just call `code_profile_spreadsheet` for the real list |

## Self-check

- [ ] I checked whether this spreadsheet is `(indexed)` or `(NOT indexed)` per the attached-files note before picking a tool family — not assumed from the file extension.
- [ ] Direct-analysis path: if the question needed an exact count/sum/filter, I used `code_run_python_code_on_document` rather than hedging about completeness — hedging is only appropriate on the indexed path.
- [ ] Indexed path: if I reported which sheets exist, I built that from retrieval results and said it might be incomplete — not from a TOC (always empty for spreadsheets).
- [ ] Indexed path: if the question needed "every row" or an aggregate (sum/count/average), I either said completeness isn't guaranteed or used the chunk count from `retrieval_get_document_info_v2` where that's actually valid (single-sheet row count).
- [ ] Indexed path: every row number I stated came from the chunk's own text/metadata, not an inference.
