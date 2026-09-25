---
name: citations-and-grounding
description: How to cite a retrieved chunk using its <citation id="..."/> tag so the Sources strip shows exactly the chunks that support the answer. Use this whenever answering from retrieval_retrieve_document_v2 results, or when the user asks where an answer came from, to cite sources, or which page or section something is on.
compatibility: Citations are collected server-side by streaming_agent.py from real tool results; only chunks you tag are shown to the user, and only a tag copied exactly from a real chunk is recognized.
---

# Citations and grounding

The citation list is **built by the server from actual tool results**, not from anything you say. Every chunk `retrieval_retrieve_document_v2` returns you carries a `cite_as` field — something like `<citation id="a1b2c3..."/>` — a tag unique to that exact chunk. To cite a claim, you copy that exact tag, character-for-character, from the chunk it came from. That's the entire mechanism.

The server then keeps **only the chunks whose tag actually appears in your answer** and shows those as the clickable "Sources" strip, translated into clean `[1]`, `[2]` chips for display. So the tags are not decoration — they are the filter that decides what the user sees, and copying the wrong one shows the wrong source.

## The one rule: copy the tag from the chunk you are actually citing

Right after a claim that comes from a specific chunk, copy that chunk's own `cite_as` tag — nothing else.

- **Copy it, don't compose it.** Never type `<citation id="...">` from memory or guess at an id. Take it verbatim from the `cite_as` field of the chunk you are citing *right now*.
- **Match the tag to the claim, not to "a chunk I saw."** If you retrieved several chunks this turn and only one of them actually supports the sentence you're writing, use *that* chunk's tag — not the first one you saw, not the one that seems related, not whichever tag you remember. A tag from the wrong chunk shows the user the wrong source next to a correct-sounding claim, which is worse than showing no source at all.
- **Citing the same chunk again** → copy the same tag again. It resolves to the same number automatically.
- **An id you can't find in any chunk you retrieved is worthless** — if you're not looking at the exact `cite_as` string from a real chunk, don't write a tag at all.

You never see or write a plain `[1]`-style number yourself. That conversion happens automatically after you submit your answer — writing one manually does nothing (it's not a recognized tag) and won't show as a source.

## Why copying, not counting

This system deliberately does not ask you to number citations in the order you saw chunks. Counting a running position across several retrieval calls, possibly a dozen or more candidate chunks over multiple retries, is easy to get wrong — the danger is a citation that *looks* plausible (a valid, real source) while actually pointing at a different chunk than the one you meant, and there's no visible sign anything went wrong. Copying an id string avoids that failure mode: either you copied the right chunk's tag, or a typo means nothing shows up. A dropped citation is a visibly safe failure; a citation pointing at the wrong source is not.

## What to tag, and what not to

Tag a claim when you could quote the specific chunk you're copying the id from to support it.

Do **not** tag:
- Your own synthesis across several chunks ("overall the trend is upward" when no single chunk says that).
- General knowledge or background you supplied yourself.
- A chunk that was returned but didn't actually support the claim — retrieval returning something is not evidence it's relevant. An untagged chunk disappears from the strip, which is exactly what should happen to a near-miss.

If a sentence mixes a retrieved fact and your own inference, tag only the retrieved part, or split the sentence.

## Consequences of getting it wrong

| Mistake | What the user sees |
|---|---|
| Tagging every chunk retrieved | A strip full of irrelevant sources — the feature becomes noise |
| Tagging nothing at all | Fallback: **all** retrieved chunks are shown unfiltered (a safety net so sources never vanish entirely — don't rely on it) |
| Copying a tag from a different chunk than the one you're citing | A source that contradicts or doesn't mention the claim, one click away — this is the specific failure this tag-based design exists to prevent, so it still requires care from you: the tag has to come from the chunk that actually supports the sentence |
| Writing a made-up or remembered id | Silently dropped — no source shown for that claim |

## Don't duplicate what the strip already shows

The strip already displays the document name, page/section, chunk tags, and a link that opens the source PDF at the right page. Your prose doesn't need "according to report.pdf on page 4…" — just place the tag and let the UI carry the provenance. Keep the sentence about the finding.

## When there's nothing to cite

If the answer didn't come from `retrieve_document_v2` chunks — you answered from `retrieval_get_table_of_contents_v2`/`retrieval_get_document_info_v2` metadata, a database query, or your own knowledge — use no tags. No strip appears, which is correct.

## If the user asks "where did that come from?"

Don't reconstruct provenance from memory. Re-retrieve so the grounding is real, then cite it using that fresh chunk's own tag. If they want to explore the document's structure rather than ask a specific question, use `retrieval_get_table_of_contents_v2` (see the `rag-retrieval-strategy` skill).

## Self-check

- [ ] Every claim traceable to a chunk has that exact chunk's `cite_as` tag right after it.
- [ ] Each tag was copied from the chunk that actually supports the sentence it follows — not from a different chunk I happened to see this turn.
- [ ] No tag sits on my own inference or general knowledge.
- [ ] I never wrote a tag's id from memory — only ever copied one directly from a chunk's `cite_as` field.
