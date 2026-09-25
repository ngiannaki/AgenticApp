---
name: web-search
description: When and how to use the retrieval server's web_search and fetch_page tools — searching the open web for facts outside the user's indexed documents, choosing when a snippet is enough vs. when to fetch the full page, and how to attribute what came back. Use this before calling web_search or fetch_page, or when deciding whether a question needs the web at all vs. retrieve_document_v2.
compatibility: Requires the retrieval MCP server (backend/mcp_servers/retrieval), web_search.py. No API key — backed by DuckDuckGo (ddgs) and trafilatura, so results/uptime aren't SLA-backed; a transient backend failure is expected occasionally, not a bug.
---

# Web search

`web_search` and `fetch_page` reach the open web. They exist for information that isn't in any document the user has uploaded — current events, general facts, documentation, anything outside this backend's own indexed knowledge. They are not a replacement for `retrieval_retrieve_document_v2`.

## Step 1 — Decide the source before searching

| The question is about | Use |
|---|---|
| A document the user uploaded ("the report", "this PDF", "what I gave you") | `retrieval_retrieve_document_v2` (see `rag-retrieval-strategy` skill) — check there first |
| Something outside any uploaded document (current events, general facts, "what is X", documentation) | `web_search` |
| Both — a document claim you need to verify or update against current reality | Retrieve from the document first, then `web_search` only for the part it can't cover |

Don't reach for `web_search` just because it's available — only call it when the answer genuinely isn't something you (or an indexed document) already have.

## Step 2 — Search, then judge the snippets

```
web_search(query="<the information you need>", max_results=5)
```

Write the query as the information you're after, not a copy of the user's sentence — same principle as retrieval queries.

Each result is `{title, url, snippet}`. For each one, ask: *does the snippet itself state the answer, or does it just look like the right page?* A snippet that names the topic without giving the fact is not an answer — it's a lead.

## Step 3 — Fetch the page when the snippet isn't enough

```
fetch_page(url="<a result's url>")
```

Call this whenever you need more than the snippet gives you: a specific number, a quote, a multi-paragraph explanation, or anything the snippet only gestures at. Don't answer from a title+snippet alone if the user's question needs detail the snippet doesn't contain — fetch the page instead of guessing what it probably says.

`fetch_page` returns extracted readable text (`content`), possibly `truncated` if the page was long. If `truncated` is true and what you needed isn't in the returned text, say so rather than assuming the missing part doesn't exist.

`fetch_page` only works on `http(s)` URLs and refuses URLs that resolve to internal/private addresses — this is a safety boundary, not a bug to work around. If a fetch fails, treat it as "couldn't retrieve that page," not as evidence about the page's content.

## Step 4 — Attribute what you use

Web results have no `cite_as` tag (that mechanism is specific to indexed-document chunks — see `citations-and-grounding`). Instead, name the source in prose: mention the site/page when you state something you got from a search or fetched page (e.g. "According to python.org, ..."), and include the URL if the user would plausibly want to open it themselves.

Never present a web result as if it came from the user's own document, and never blend an unverified web claim with a document-grounded one in a way that makes them look equally sourced — keep the two kinds of evidence distinguishable in your answer.

## When search comes back empty or fails

`web_search`/`fetch_page` can fail (`{success: false, error, error_category}`) — rate limiting, network issues, a disallowed URL. This has no SLA behind it (no paid API key), so an occasional failure is expected, not a sign something is broken. On failure:

- Don't retry the identical query/URL more than once.
- Try one reworded query before giving up (same vocabulary-mismatch logic as document retrieval).
- If it still fails, say so plainly ("I couldn't reach the web search for this") rather than answering from memory and presenting it as sourced from the web.

## Self-check

- [ ] I checked whether this was really a web question, not something an indexed document already answers.
- [ ] I didn't answer from a snippet alone when the question needed more detail than the snippet gave.
- [ ] Anything I stated from a web result is attributed to its source in prose, not left looking like my own knowledge or a document citation.
- [ ] A search/fetch failure was reported honestly, not papered over with an unsourced guess.
