---
name: rag-retrieval-strategy
description: The procedure for answering a question from an indexed document — decompose the question, scope the search, retrieve, check whether the retrieved chunks actually cover every part of the question, retry with a changed query/top_k/filter if they don't, and answer only what the evidence supports. Use this whenever answering from retrieval_retrieve_document_v2, retrieval_get_table_of_contents_v2, or retrieval_get_document_info_v2, and especially before concluding that a document doesn't contain something.
compatibility: Requires the retrieval MCP server (backend/mcp_servers/retrieval), v2 pipeline. Documents indexed only with the v1 tools are invisible to these tools.
---

# RAG retrieval procedure

Retrieval is a search, not an oracle. It returns chunks that are *similar* to your query — similarity is not the same as *answering*. Your job is to decide whether what came back actually answers the question, and to do something different if it doesn't.

## Step 1 — Decompose the question before searching

Write down (to yourself) the distinct facts you need. "How did revenue change and why?" is two: (a) the revenue numbers, (b) the stated reason. A single query rarely covers both well.

If the question has multiple parts, expect to run more than one retrieval — one good query per fact beats one broad query trying to catch everything.

## Step 2 — Scope before you search

If the user named a section, a page, a heading, or "the table," call `retrieval_get_table_of_contents_v2(document_id)` first and use what it returns to filter:

| The user says | Do this |
|---|---|
| "in the methods section" | TOC → `filters={"h1": "<exact heading from TOC>"}` |
| "on page 7" | `filters={"page": 7}` |
| "the pricing table", "only the tables" | `filters={"chunk_content_type": "table"}` |
| "what's in this document" | TOC alone may answer it — don't search yet |
| nothing structural | plain query, no filters |

Use the heading string **exactly** as the TOC returned it. A filter with a heading you invented matches nothing and returns zero chunks — which looks identical to "the document doesn't cover this."

## Step 3 — Retrieve

```
retrieval_retrieve_document_v2(document_id, query="<what you need to find>", top_k=5)
```

Write the query as the *information you're looking for*, not a copy of the user's sentence. "What were Q3 revenue figures" retrieves better than "can you tell me about the stuff in q3".

## Step 4 — Coverage check (do not skip this)

For each fact from Step 1, classify what came back:

- **DIRECT** — a chunk explicitly states it. Use it, cite it.
- **PARTIAL** — a chunk is about the right topic but doesn't state the specific fact (mentions "revenue grew" when you need the number). Not an answer. Go to Step 5.
- **ABSENT** — nothing relevant. Go to Step 5.

The test for DIRECT is: *could you quote the chunk to support this claim?* If you'd have to infer, extrapolate, or fill a gap with your own knowledge, it is not DIRECT.

## Step 5 — Retry, changing exactly one thing

**Retrieval is deterministic.** The same `document_id` + query + `top_k` + filters returns the same chunks every time. Re-running an identical call is pure waste — if you retry, something must change. Change them in this order (most effective first):

1. **Reword the query using vocabulary you saw in the returned chunks.** Vocabulary mismatch is the single most common cause of a miss — the document says "turnover," you searched "revenue." This fixes more misses than anything else and costs nothing.
2. **Relax or drop a filter you applied.** A too-narrow filter is a self-inflicted miss. If you filtered to `h1="Results"` and got nothing useful, drop it before blaming the document.
3. **Raise `top_k`**: 5 → 12 → 25 (25 is the hard ceiling; higher values are clamped).
4. **Re-scope via the TOC** if the outline suggests the content lives somewhere you haven't looked.

Caution on large `top_k`: chunks come back with neighboring chunks spliced in, so a `top_k=25` result can exceed the 8000-character cap on what a tool result carries into conversation memory and get truncated. Prefer a better query over a bigger `top_k`.

## Step 6 — Stop condition

**Budget: 3 retrieval attempts per fact.** After that, stop searching and report honestly:

> "The document covers X [2], but I couldn't find anything stating Y — it may not be in this document, or it may be phrased in a way my searches missed."

Do **not**:
- Keep retrying past the budget (the turn has a hard 15-iteration ceiling; exhausting it on retrieval means no iterations left to actually finish the task).
- Fill the gap with your own general knowledge and present it as if it came from the document.
- Claim "the document does not contain X" after a single query. You may say that only after at least two genuinely different searches came back ABSENT.

## Step 7 — Answer and cite only what supports the answer

Cite the chunks that actually support each claim, copying each one's own `cite_as` tag as described in the `citations-and-grounding` skill. Chunks you retrieved but didn't use should **not** get a tag — uncited chunks are filtered out of the Sources strip entirely, which is what keeps it meaningful.

## Self-check before you answer

- [ ] Every fact from Step 1 is either answered from a DIRECT chunk, or explicitly reported as not found.
- [ ] No claim rests on a PARTIAL chunk that I stretched into an answer.
- [ ] Every claim from a chunk has a marker; nothing I inferred myself has one.
- [ ] If I said the document lacks something, I searched for it at least twice, differently.

## Anti-patterns

| Don't | Why |
|---|---|
| Re-run the identical query | Deterministic — same result, wasted iteration |
| Treat "topically related" as an answer | That's the main way wrong answers get cited as grounded |
| Say "not in the document" after one query | Usually a vocabulary mismatch, not an absence |
| Jump straight to `top_k=25` | Slower, truncation-prone, and usually fixes nothing a better query wouldn't |
| Cite every chunk retrieved | Makes the Sources strip noise |

## Worked example

> **User:** "What does the report say about churn in Q3, and what did they blame it on?"

1. Two facts: churn figure, stated cause.
2. Not structural → no TOC needed yet.
3. `retrieve(query="Q3 churn rate", top_k=5)` → one chunk gives "churn reached 7.2% in Q3" (DIRECT for fact 1). Nothing on cause (ABSENT for fact 2).
4. Retry for fact 2 only, rewording with the document's own vocabulary seen in the first result — it said "attrition," not "churn": `retrieve(query="reasons for attrition increase", top_k=12)` → chunk explains onboarding delays (DIRECT).
5. Answer both, marker on each, nothing else cited.
