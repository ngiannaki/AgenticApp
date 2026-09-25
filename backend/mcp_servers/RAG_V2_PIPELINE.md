# V2 RAG pipeline reference

Snapshot of what the LangChain-native (`v2`) indexing/retrieval pipeline
actually does today. Not a design doc — see git history / code comments for
rationale. Source: `backend/mcp_servers/indexing/langchain_*.py`,
`backend/mcp_servers/retrieval/langchain_pipeline.py`,
`backend/config/settings.py` (`LangchainRagSettings`, env prefix `RAG_V2_`).

v1 (llama_index-based, `upload_and_index_document` / `retrieve_document`) is
a fully separate pipeline with its own Chroma collections (`doc_*` vs
`docv2_*`). This doc covers v2 only (`*_v2` tools).

## Indexing (`upload_and_index_document_v2`)

```
file
  -> loader (per content_type)
  -> [pdf/docx/txt only] structure-preserving split
     -> table blocks kept atomic (row-batched if oversized)
     -> prose split on headings (MarkdownHeaderTextSplitter) then by
        character count (RecursiveCharacterTextSplitter)
     -> section breadcrumb prepended to each prose chunk's text
  -> [csv/xlsx/pptx] plain RecursiveCharacterTextSplitter
  -> metadata stamped (document_id, document_name, chunk_index, chunk_id,
     user_id, chunk_content_type, h1/h2/h3, page/sheet/row, ...)
  -> embedded (backend.config.get_langchain_embedding — Ollama in DEV,
     OpenAI in PROD)
  -> written to Chroma collection `docv2_{document_id}`
     (collection metadata carries a JSON-encoded `toc` when headings were
     found)
```

**Loaders** (`langchain_loaders.py`, dispatched by detected `content_type`):

| type | primary path | fallback |
|---|---|---|
| pdf | `langchain_structure.pdf_to_documents` — markdown per page via `pdf_engine` | plain PyMuPDF text extraction (`parsers/pdf.py`) |
| docx | `parsers/docx_reader.py::parse_docx` — markdown with `#` headings + inline tables | `Docx2txtLoader` (plain text), then plain text extraction |
| pptx | `UnstructuredPowerPointLoader` | `parsers/pptx_reader.py` (per-slide plain text) |
| csv | `CSVLoader` (one Document/row) | `parsers/csv_reader.py` (one Document/row) |
| xlsx | `UnstructuredExcelLoader` (mode="elements") | `parsers/xlsx_reader.py` (one Document/row) |
| txt/md/log | `TextLoader` | `parsers/txt_reader.py` |

csv/xlsx Documents are always tagged `chunk_content_type=table` regardless
of loader path (whole-file-is-tabular).

**PDF engines** (`settings.rag_v2.pdf_engine`, env `RAG_V2_PDF_ENGINE`):
- `docling` (default) — `parsers/document_parser.py::HierarchicalParser`.
  Layout-aware, per-page markdown, real table cell structure, h1/h2/h3
  hierarchy. Heavier (loads a model pipeline once per process).
- `pymupdf4llm` — `pymupdf4llm.to_markdown(..., page_chunks=True)`. Faster,
  dependency-light, markdown output (headings + tables) but less precise
  table/layout detection than docling.
- Either engine falls back to plain PyMuPDF text extraction
  (`langchain_loaders._fallback_pdf`) if its import or parse fails, or
  produces no pages.

**Structure-preserving split** (`langchain_structure.split_markdown_preserving_structure`,
used for pdf/docx/txt only — csv/xlsx/pptx use the plain character splitter
since those Documents are already row/slide-atomic):
1. Table blocks are found by their GFM separator row (`|---|---|` or
   `--- | ---`, pipe-wrapped or not) immediately following a header row —
   the unambiguous part of table syntax regardless of source format.
2. A table under `chunk_size * 2` chars is one chunk
   (`chunk_content_type=table`); a larger one is split into row-batches with
   the header row repeated at the top of each batch.
3. Non-table text is split on `#`/`##`/`###` headings
   (`MarkdownHeaderTextSplitter`, headers captured into `h1`/`h2`/`h3`
   metadata and stripped from the body), then character-split within each
   section (`RecursiveCharacterTextSplitter`, `chunk_size`/`chunk_overlap`).
   Each resulting chunk (`chunk_content_type=prose`) gets a `"Section: h1 >
   h2\n\n"` breadcrumb prepended to its text — this is embedded, not just
   metadata, so section context affects retrieval relevance too.
4. Headings seen (with the page they appeared on) are collected across the
   whole document into a deduped, ordered list and stored as
   `collection_metadata["toc"]` (JSON string — Chroma collection metadata
   only accepts scalars) for `get_table_of_contents_v2` to read back.

## Retrieval (`retrieve_document_v2`)

```
query
  -> dense: Chroma similarity search (k = fetch_k, optional `where` filter)
  -> sparse: BM25 over the collection's chunks (pre-filtered the same way)
  -> EnsembleRetriever fuses dense + sparse (dense_weight / sparse_weight)
  -> [if use_reranker] CrossEncoderReranker cuts fetch_k candidates down to top_k
  -> neighbor expansion: splice in ±neighbor_window adjacent chunks
     (by chunk_index) for extra context
  -> grounding block attached to every returned chunk
```

- `fetch_k = clamp(top_k * fetch_k_multiplier, 10, max_fetch_k)` — the pool
  size both retrievers draw from before fusing/reranking. Reranking, not
  truncation, decides what survives to `top_k`.
- `filters` (optional, exact-match): applied identically to the dense leg
  (Chroma `where`) and the sparse leg (pre-filter before building
  `BM25Retriever`). Common keys: `chunk_content_type` (`"table"`/`"prose"`),
  `page`, `sheet`, `h1`/`h2`/`h3`. Call `get_table_of_contents_v2` first to
  see what sections/pages exist before filtering on them.
- Neighbor expansion (on by default): for each returned hit, looks up
  `chunk_index ± neighbor_window` within the same `source` from the
  collection's already-loaded chunks (no extra Chroma round-trip) and
  merges their text in. `text` becomes the merged block;
  `grounding.expanded` says whether merging happened for that chunk.
- Every chunk's `grounding` block: `document_id`, `document_name`, `source`,
  `location` (page/slide/sheet/row, whichever is set), `chunk_id`,
  `chunk_index`, `content_type` (file type: pdf/docx/...),
  `chunk_content_type` (`table`/`prose`), `section` (`"h1 > h2"`, or `None`),
  `expanded`.

`get_table_of_contents_v2(document_id)` reads the collection's stored `toc`
(list of `{level, title, page}`); falls back to page/sheet coverage
(`get_document_info_v2`'s `total_pages`/`sheets`) for documents with no
captured outline (csv/xlsx/pptx, or one indexed before this existed) — in
that case `toc` is `[]`.

## `LangchainRagSettings` (env prefix `RAG_V2_`)

| setting | default | controls |
|---|---|---|
| `collection_prefix` | `docv2_` | Chroma collection name = prefix + document_id |
| `pdf_engine` | `docling` | `docling` \| `pymupdf4llm` — see above |
| `chunk_size` | `1000` | target chunk size (chars) for both the structure-preserving and plain splitters |
| `chunk_overlap` | `150` | overlap (chars) between adjacent chunks |
| `default_top_k` | `8` | chunks returned when the caller omits `top_k` |
| `max_top_k` | `25` | hard ceiling on a caller-supplied `top_k` |
| `fetch_k_multiplier` | `6` | candidates fetched per retriever = `top_k * this`, before fusing/reranking |
| `max_fetch_k` | `60` | hard ceiling on computed `fetch_k` |
| `dense_weight` / `sparse_weight` | `0.5` / `0.5` | `EnsembleRetriever` fusion weights |
| `use_reranker` | `True` | rerank fused candidates with a cross-encoder before truncating to `top_k` |
| `rerank_model` | `cross-encoder/ms-marco-MiniLM-L-12-v2` | HuggingFace cross-encoder model id |
| `neighbor_expansion` | `True` | splice in adjacent chunks around each hit |
| `neighbor_window` | `1` | how many chunks on each side to splice in |

All of the above are set once via `.env` / environment and apply to every
call. Per-call knobs (not config): `top_k`, `filters`, `user_id` — passed as
arguments to `retrieve_document_v2` / `upload_and_index_document_v2`.
