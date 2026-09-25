---
name: document-ingestion
description: What indexing actually does to an uploaded file, which file types are supported, and how to diagnose a document that returns few or no results instead of blaming the search. Use this when a user asks why an upload isn't searchable, why answers from a document seem thin or empty, what file types are supported, or why a file was rejected.
compatibility: Describes backend/mcp_servers/indexing's v2 (LangChain) pipeline. A document indexed with the v1 tools is invisible to the _v2 retrieval tools and vice versa — separate Chroma collections.
---

# Document ingestion

Use this when retrieval on a specific document behaves oddly. The failure is often at ingestion time, and the symptom shows up at retrieval time — so diagnose in that order rather than assuming the query was bad.

## Supported types

`pdf`, `docx`, `pptx`, `csv`, `xlsx`, `xlsm`, `xls`, `ods`, `txt`, `md`, `log`.

Legacy `.doc` and `.ppt` are **rejected by design** with an explicit error telling the user to save as `.docx`/`.pptx`. That's expected behavior, not a bug — relay the conversion instruction rather than investigating.

Images, archives (`.zip`/`.tar`), and JSON are **not ingestable**. Say so plainly; don't improvise a workaround.

## What each type produces (this determines what retrieval can find)

| Type | Chunking | Notable |
|---|---|---|
| pdf | Per-page markdown; headings and tables preserved | Scanned PDFs (no text layer) are auto-detected and routed through OCR |
| docx | Markdown headings + inline tables | Yields a real heading outline for the TOC tool |
| csv/xlsx/xlsm/xls/ods | One chunk per row, always tagged `chunk_content_type="table"` | No prose chunks exist in a spreadsheet, by definition |
| pptx | One chunk per slide, plain text | **No** `chunk_content_type` tagging — a table-only filter finds nothing in a pptx even if a slide shows a table |
| txt/md/log | Plain text, heading-split if markdown-style headings exist | |

Two consequences worth remembering:
- A `filters={"chunk_content_type": "table"}` search against a **pptx** returns zero results regardless of content. Don't report that as "the deck has no tables."
- A spreadsheet has **no** `prose` chunks, so a prose filter against one is equally empty.

## Diagnosing a thin or empty document

Run these in order before concluding anything:

1. **`retrieval_get_document_info_v2(document_id)`** — did it index at all, and how many chunks?
   - Reasonable chunk count → ingestion was fine. The problem is the query; go to `rag-retrieval-strategy` and retry properly.
   - Zero / near-zero chunks → ingestion produced almost nothing. Continue below.
2. **`retrieval_get_table_of_contents_v2(document_id)`** — empty `toc` means either no headings were captured (normal for csv/xlsx/pptx) or the document has no heading structure. Not itself an error.
3. Interpret the low chunk count by type:
   - **PDF** → most likely a scanned document whose OCR pass produced little usable text (poor scan quality, handwriting, heavy graphics). Tell the user the PDF appears to be a scan and the text extraction came back sparse — a clearer scan or a text-based PDF would fix it.
   - **Spreadsheet** → the file may be effectively empty, or the data may start well below the header row.
   - **Anything else** → the source file itself likely has very little extractable text.

## What not to do

| Don't | Why |
|---|---|
| Blame the search when chunk count is ~0 | The content was never indexed; no query can find it |
| Say "this document doesn't mention X" without checking it indexed | You'd be describing an ingestion failure as a content fact |
| Report "no tables found" for a pptx table-filter miss | pptx chunks are never table-tagged — the filter can't match |
| Suggest re-uploading as a fix without a reason | Re-running the same pipeline on the same bytes gives the same result |

## Uploading

Ingestion (`indexing_upload_and_index_document_v2`) is normally driven by the frontend's upload flow, not something to call mid-conversation unless the user is handing you file content to index right now. It needs a `filename` (used for type detection) plus either base64 bytes or a `document_id` already in the shared document store. Never invent either value.

Note for already-uploaded documents: there is no server-local `file_path` for them, so `indexing_read_document` will not work — use the `_v2` retrieval tools instead.
