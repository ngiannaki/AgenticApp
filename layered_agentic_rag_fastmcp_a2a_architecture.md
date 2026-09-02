# Layered Agentic RAG Architecture — FastMCP + A2A

## Executive recommendation

Use **MCP as the capability layer** and **A2A as the agent-to-agent coordination layer**.

Start with:

- **Indexing MCP** — parsing, chunking, embedding, indexing
- **Retrieval MCP** — vector/keyword/hybrid search, filtering, reranking
- **Retrieval Agent** — plans searches, evaluates evidence, retries when necessary
- **Synthesis Agent** — produces a grounded answer from evidence
- **Orchestrator Agent** — receives the user question and delegates work through A2A

The architecture should be:

```text
USER
  |
  v
A2A ORCHESTRATOR
  |
  | A2A
  v
RETRIEVAL AGENT
  |
  | MCP
  v
RETRIEVAL MCP
  |
  +--> Vector DB
  +--> Keyword/Search DB
  +--> Metadata DB
  |
  v
EVIDENCE
  |
  | A2A
  v
SYNTHESIS AGENT
  |
  v
ANSWER + CITATIONS
```

The ingestion side is separate:

```text
DOCUMENT
   |
   v
INDEXING MCP
   |
   +--> parse
   +--> normalize
   +--> chunk
   +--> embed
   +--> index
   |
   v
KNOWLEDGE STORES
```

The important rule is:

> **MCP = capability boundary. A2A = agent boundary.**

Do not make every MCP server into an agent.

---

# 1. Why this architecture?

There are three different responsibilities.

## MCP

MCP should expose capabilities such as:

```text
search(...)
get_document(...)
ingest_document(...)
reindex_document(...)
```

MCP hides infrastructure details from agents.

For example, an agent should not know whether retrieval uses:

```text
Qdrant
Pinecone
Weaviate
Postgres/pgvector
Elasticsearch
OpenSearch
```

It should simply call:

```text
retrieval.search(...)
```

## Agent

An agent is useful when there is reasoning or planning.

For example:

```text
Question
   |
   v
What should I search?
   |
   v
Search
   |
   v
Is the evidence sufficient?
   |
   +---- no ----> reformulate
   |
   +---- yes ---> return evidence
```

## A2A

A2A is the communication boundary between agents.

For example:

```text
Orchestrator Agent
       |
       | A2A task
       v
Retrieval Agent
```

---

# 2. Recommended agents

Start with three logical agents.

## 2.1 Orchestrator Agent

Responsibilities:

- receive user request
- determine intent
- decide which agent should handle the request
- delegate through A2A
- collect results
- return the final response

It should NOT:

- query the vector DB directly
- parse documents
- implement chunking
- implement retrieval algorithms
- contain database-specific code

Think of it as the **traffic controller**, not the worker.

---

## 2.2 Retrieval Agent

This is where the first useful agentic behavior appears.

It should:

1. understand the question
2. create search queries
3. call Retrieval MCP
4. inspect results
5. decide whether evidence is sufficient
6. reformulate and search again if necessary
7. detect contradictions
8. return structured evidence

Example:

```text
Question:
"Compare our EU and US data retention policies."

Retrieval Agent:

Search 1:
"EU data retention policy"

Search 2:
"US data retention policy"

Inspect results.

US evidence is weak.

Search 3:
"US data retention latest policy"

Evaluate.

Return evidence grouped into:
- EU
- US
```

That is a much better use of an agent than simply having an LLM call `search()` once.

---

## 2.3 Synthesis Agent

This agent receives:

```text
user question
+
retrieved evidence
```

and produces:

```text
grounded answer
+
citations
+
uncertainty
```

It should be explicitly instructed:

```text
Only make claims supported by the supplied evidence.

If evidence is insufficient, say so.

Do not invent citations.

Preserve contradictions instead of silently choosing a source.
```

---

# 3. MCP servers

Start with exactly two.

## 3.1 Indexing MCP

Public tools could be:

```text
ingest_document
delete_document
reindex_document
get_document_status
```

Internally:

```text
ingest_document
      |
      v
parse
      |
      v
normalize
      |
      v
chunk
      |
      v
metadata enrichment
      |
      v
embedding
      |
      v
index
```

The agent does not need to know about any of these internal stages.

---

## 3.2 Retrieval MCP

Start with:

```text
search
get_chunk
get_document
```

A good `search` interface:

```python
async def search(
    query: str,
    top_k: int = 10,
    search_mode: str = "hybrid",
    filters: dict | None = None,
):
    ...
```

Possible modes:

```text
vector
keyword
hybrid
```

The MCP can internally perform:

```text
query
 |
 +--> vector search
 |
 +--> keyword search
 |
 v
merge
 |
 v
deduplicate
 |
 v
filter
 |
 v
rerank
 |
 v
top-k
```

---

# 4. Structured evidence

This is extremely important.

Do not return only:

```python
list[str]
```

Return structured evidence.

For example:

```python
class RetrievalResult:
    chunk_id: str
    document_id: str
    text: str
    score: float
    metadata: dict
```

Metadata might include:

```python
{
    "title": "Security Policy",
    "page": 14,
    "section": "Data Retention",
    "version": "2026.2"
}
```

Then your synthesis agent can cite:

```text
[Security Policy, p.14]
```

without trying to reconstruct the source from raw text.

---

# 5. Suggested domain models

Use Pydantic or another explicit schema system.

## Document

```python
class Document:
    id: str
    source: str
    title: str
    content_type: str
    metadata: dict
    version: str
```

## Chunk

```python
class Chunk:
    id: str
    document_id: str
    text: str
    position: int
    metadata: dict
```

## Retrieval result

```python
class RetrievalResult:
    chunk_id: str
    document_id: str
    text: str
    score: float
    metadata: dict
```

## Retrieval response

```python
class RetrievalResponse:
    answerable: bool
    evidence: list[RetrievalResult]
    searches_performed: list[str]
    confidence: float
    missing_information: list[str]
```

Keep these models independent of FastMCP, A2A, and your vector database.

---

# 6. Repository layout 

I recommend a monorepo initially.

```text
agentic-rag/
|
├── pyproject.toml
├── README.md
├── .env.example
|
├── apps/
|   |
|   ├── orchestrator/
|   |   ├── main.py
|   |   ├── agent.py
|   |   ├── prompts.py
|   |   └── config.py
|   |
|   ├── retrieval_agent/
|   |   ├── main.py
|   |   ├── agent.py
|   |   ├── prompts.py
|   |   └── config.py
|   |
|   └── synthesis_agent/
|       ├── main.py
|       ├── agent.py
|       ├── prompts.py
|       └── config.py
|
├── mcp_servers/
|   |
|   ├── indexing/
|   |   ├── server.py
|   |   ├── tools.py
|   |   ├── pipeline.py
|   |   ├── parsers/
|   |   ├── chunking/
|   |   └── embeddings/
|   |
|   └── retrieval/
|       ├── server.py
|       ├── tools.py
|       ├── retriever.py
|       ├── reranker.py
|       └── filters.py
|
├── core/
|   ├── models/
|   |   ├── document.py
|   |   ├── chunk.py
|   |   ├── retrieval.py
|   |   └── agent.py
|   ├── services/
|   └── config.py
|
├── infrastructure/
|   ├── vector_store/
|   ├── document_store/
|   ├── object_store/
|   └── llm/
|
├── tests/
|   ├── unit/
|   ├── integration/
|   ├── agents/
|   ├── retrieval/
|   └── evaluation/
|
└── scripts/
    ├── ingest.py
    └── dev.py
```

---

# 7. FastMCP design

Keep MCP servers thin.

Conceptually:

```python
from fastmcp import FastMCP

mcp = FastMCP("retrieval")

@mcp.tool
async def search(
    query: str,
    top_k: int = 10,
    search_mode: str = "hybrid",
):
    return await retriever.search(
        query=query,
        top_k=top_k,
        search_mode=search_mode,
    )
```

Do not put the entire retrieval implementation inside the MCP decorator.

Prefer:

```text
MCP tool
   |
   v
Application service
   |
   v
Retrieval engine
   |
   v
Database
```

That way the application logic can also be tested without MCP.

---

# 8. Indexing implementation

The indexing pipeline should be deterministic wherever possible.

```text
Document
   |
   v
Parser
   |
   v
Normalized document
   |
   v
Chunker
   |
   v
Chunks
   |
   v
Metadata enrichment
   |
   v
Embedding model
   |
   v
Vector index
```

Example:

```python
async def ingest_document(path: str):

    raw = parser.parse(path)

    document = normalizer.normalize(raw)

    chunks = chunker.chunk(document)

    embeddings = await embedder.embed(
        [chunk.text for chunk in chunks]
    )

    await index.store(
        chunks=chunks,
        embeddings=embeddings,
    )

    return document
```

Avoid making this agentic initially.

---

# 9. Why indexing usually should NOT be an agent

This is an important architectural distinction.

Normally:

```text
upload
 -> parse
 -> chunk
 -> embed
 -> index
```

is a pipeline, not a reasoning problem.

An agent adds value only when the system must make decisions such as:

```text
What kind of document is this?

Which parser should be used?

Is this a contract?

Is this an invoice?

Does it require OCR?
```

Even then, the agent should select deterministic capabilities rather than implement them itself.

---

# 10. Retrieval implementation

Start with hybrid retrieval.

```python
async def search(query, top_k=10, filters=None):

    vector_results = await vector.search(
        query,
        top_k=30,
    )

    keyword_results = await keyword.search(
        query,
        top_k=30,
    )

    merged = merge(vector_results, keyword_results)

    filtered = apply_filters(
        merged,
        filters,
    )

    ranked = await reranker.rank(
        query,
        filtered,
    )

    return ranked[:top_k]
```

The agent should never need to know this implementation.

It only sees:

```text
search(...)
```

---

# 11. Retrieval Agent loop

A strong first implementation is:

```text
                    QUESTION
                        |
                        v
                    PLAN
                        |
                        v
                     SEARCH
                        |
                        v
                  ASSESS RESULTS
                        |
              +---------+---------+
              |                   |
           enough              insufficient
              |                   |
              v                   v
           RETURN             REFORMULATE
                                  |
                                  v
                                SEARCH
```

Pseudo-code:

```python
async def retrieve(question):

    plan = await llm.plan_retrieval(question)

    evidence = []

    for query in plan.queries:

        results = await retrieval_mcp.search(
            query=query,
            top_k=10,
            filters=plan.filters,
            search_mode="hybrid",
        )

        evidence.extend(results)

    assessment = await llm.assess_evidence(
        question=question,
        evidence=evidence,
    )

    if not assessment.sufficient:

        followup = await llm.reformulate(
            question=question,
            missing_information=assessment.missing_information,
        )

        results = await retrieval_mcp.search(
            query=followup,
            top_k=10,
            search_mode="hybrid",
        )

        evidence.extend(results)

    return deduplicate(evidence)
```

Put hard limits around this:

```python
MAX_ITERATIONS = 3
MAX_RETRIEVAL_CALLS = 6
MAX_CONTEXT_CHUNKS = 30
```

---

# 12. Orchestrator design

The orchestrator should be deliberately thin.

Conceptually:

```python
async def handle_question(question):

    route = await route_question(question)

    if route.requires_retrieval:

        retrieval = await retrieval_agent.run(
            question=question,
        )

        answer = await synthesis_agent.run(
            question=question,
            evidence=retrieval.evidence,
        )

        return answer

    return await direct_answer(question)
```

The important point is that:

```text
Orchestrator
    |
    | A2A
    v
Retrieval Agent
```

rather than:

```text
Orchestrator
    |
    +--> vector DB
    +--> embeddings
    +--> parser
```

---

# 13. A2A boundary

Treat agents as independent services.

The orchestrator should know:

```text
agent identity
agent capabilities
agent endpoint
task schema
result schema
```

It should not know:

```text
vector DB
embedding provider
chunking strategy
reranker
retrieval implementation
```

For version 1, hard-code the Retrieval Agent endpoint.

Later you can introduce dynamic agent discovery.

---

# 14. Security

Security should be enforced by services, not prompts.

For example, do not rely on:

```text
"Only retrieve documents from tenant A."
```

inside an LLM prompt.

Instead:

```python
await retrieval_mcp.search(
    query=query,
    tenant_id=authenticated_tenant,
)
```

The server must enforce:

```text
tenant
permissions
document collections
security classification
metadata filters
```

This is especially important in multi-tenant RAG.

---

# 15. Contradiction handling

Your Retrieval Agent should explicitly detect contradictions.

Example:

```text
Document A:
Retention = 30 days

Document B:
Retention = 90 days
```

The agent should not simply select the first result.

It should:

```text
1. Detect contradiction.
2. Search for latest policy.
3. Search for authoritative source.
4. Compare versions/dates.
5. Return the contradiction to synthesis.
```

The Synthesis Agent can then answer:

```text
The current policy appears to be 90 days, while an older document
states 30 days. The newer policy takes precedence.
```

assuming the evidence actually supports that conclusion.

---

# 16. Citation architecture

Treat citations as data.

Example:

```python
class EvidenceItem:
    chunk_id: str
    document_id: str
    title: str
    page: int | None
    text: str
    score: float
```

Then synthesis gets:

```text
Question
+
EvidenceItem[]
```

rather than just:

```text
Question
+
large text blob
```

This makes citations, evaluation, and UI integration much easier.

---

# 17. Failure states

Do not reduce every failure to "I don't know."

Define explicit states:

```text
SUCCESS
INSUFFICIENT_EVIDENCE
TOOL_ERROR
TIMEOUT
UNAUTHORIZED
INVALID_REQUEST
```

This lets you distinguish:

```text
The KB does not contain the answer.
```

from:

```text
The Retrieval MCP failed.
```

Those are operationally very different.

---

# 18. Observability

Build tracing from the beginning.

Useful identifiers:

```text
trace_id
request_id
agent_id
task_id
tool_call_id
tenant_id
```

Track:

```text
LLM latency
LLM tokens
MCP latency
retrieval latency
number of searches
retrieval scores
number of chunks
final latency
cost
```

A trace should look like:

```text
trace-123

orchestrator
    |
    +-- retrieval-agent
           |
           +-- search("EU retention policy")
           |
           +-- search("GDPR data retention")
           |
           +-- evidence assessment
    |
    +-- synthesis-agent
           |
           +-- final answer
```

---

# 19. Evaluation

Create a fixed evaluation set:

```text
question
expected_answer
expected_sources
```

Measure retrieval:

```text
Recall@K
Precision@K
MRR
NDCG
```

Measure generation:

```text
faithfulness
answer relevance
citation correctness
context utilization
```

Measure agent behavior:

```text
tool calls
iterations
unnecessary searches
failure rate
latency
cost
```

One especially valuable metric is:

```text
For answerable questions,
did the Retrieval Agent find sufficient evidence?
```

---

# 20. Testing strategy

## Unit tests

Test independently:

```text
parser
chunker
embedder adapter
retriever
reranker
filters
```

No LLM required.

## MCP integration tests

Test:

```text
MCP client
    ->
MCP server
    ->
test storage
```

## Agent tests

Mock MCP responses.

Example:

```text
Question:
"What is our retention policy?"

Mock Retrieval MCP:
A, B, C

Expected:
agent identifies A + B as relevant
```

## End-to-end

Test:

```text
User
 -> Orchestrator
 -> Retrieval Agent
 -> Retrieval MCP
 -> Vector DB
 -> Synthesis Agent
 -> Answer
```

---

# 21. Implementation order

Do this in stages.

## Phase 1 — Basic deterministic RAG

```text
documents
 -> parser
 -> chunker
 -> embeddings
 -> vector DB
 -> retrieval
 -> LLM
 -> answer
```

Get this working first.

## Phase 2 — Indexing MCP

Expose:

```text
ingest_document
delete_document
reindex_document
```

## Phase 3 — Retrieval MCP

Expose:

```text
search
get_document
get_chunk
```

## Phase 4 — Retrieval Agent

Build:

```text
A2A Retrieval Agent
       |
       v
Retrieval MCP
```

Add bounded iterative retrieval.

## Phase 5 — Synthesis Agent

Give it:

```text
question
+
structured evidence
```

## Phase 6 — Orchestrator

Build:

```text
User
 |
 v
Orchestrator
 |
 | A2A
 v
Retrieval Agent
 |
 v
Synthesis Agent
```

## Phase 7 — Evaluation + tracing

Add:

```text
evaluation dataset
metrics
tracing
budgets
```

Only after this should you add more agents.

---

# 22. Suggested project dependencies

Keep dependency boundaries clean.

```text
FastMCP
    -> MCP transport/tooling

A2A package
    -> agent communication

LLM SDK
    -> model calls

Vector DB SDK
    -> infrastructure

Pydantic
    -> schemas

Application code
    -> domain + orchestration
```

Your domain models should not depend on FastMCP or a particular database.

---

# 23. Recommended first architecture

This is the version I would actually implement first:

```text
                         USER
                           |
                           v
                 +-------------------+
                 | A2A ORCHESTRATOR  |
                 +---------+---------+
                           |
                           | A2A
                           v
                 +-------------------+
                 | RETRIEVAL AGENT   |
                 |                   |
                 | plan              |
                 | search            |
                 | evaluate          |
                 | retry             |
                 +---------+---------+
                           |
                           | MCP
                           v
                 +-------------------+
                 |  RETRIEVAL MCP    |
                 +---------+---------+
                           |
                 +---------+---------+
                 |                   |
                 v                   v
             Vector DB          Search DB
                 |
                 v
              Evidence
                 |
                 | A2A
                 v
          +-------------------+
          | SYNTHESIS AGENT   |
          +---------+---------+
                    |
                    v
             Answer + citations


       DOCUMENT INGESTION

Documents
    |
    v
+-------------------+
|   INDEXING MCP    |
+---------+---------+
          |
          +--> parse
          +--> normalize
          +--> chunk
          +--> embed
          +--> index
```

---

# 24. Core architectural rule

Keep the boundaries crisp:

```text
MCP:
    "Here is a capability."

Agent:
    "Here is a reasoning process."

A2A:
    "Here is how agents communicate."

Orchestrator:
    "Here is who should do the work."

RAG:
    "Here is the evidence used to answer."
```

A useful rule of thumb:

> **If something can be implemented as a deterministic API call, it probably belongs in MCP/application services. If it requires deciding what to do next, it is a candidate for an agent.**

---

# 25. Concrete build checklist

- [ ] Create the monorepo and Python project.
- [ ] Define Pydantic/domain schemas.
- [ ] Implement deterministic ingestion.
- [ ] Implement vector + metadata storage.
- [ ] Implement deterministic retrieval.
- [ ] Expose indexing through FastMCP.
- [ ] Expose retrieval through FastMCP.
- [ ] Create Retrieval Agent using A2A.
- [ ] Make Retrieval Agent call Retrieval MCP.
- [ ] Add bounded iterative retrieval.
- [ ] Create Synthesis Agent.
- [ ] Pass structured evidence to Synthesis Agent.
- [ ] Create Orchestrator Agent.
- [ ] Make Orchestrator delegate through A2A.
- [ ] Add citations to evidence.
- [ ] Add authorization at service boundaries.
- [ ] Add tracing and structured logs.
- [ ] Add retrieval evaluation.
- [ ] Add end-to-end tests.
- [ ] Add timeouts and budgets.
- [ ] Only then add additional agents.

---

# 26. The mental model to keep

The cleanest way to think about your system is:

```text
                 ┌───────────────────────┐
                 │       ORCHESTRATOR    │
                 │       "WHO?"          │
                 └───────────┬───────────┘
                             |
                            A2A
                             |
                             v
                 ┌───────────────────────┐
                 │     RETRIEVAL AGENT   │
                 │       "WHAT?"         │
                 │                       │
                 │ What should I search? │
                 │ Do I need another?   │
                 └───────────┬───────────┘
                             |
                            MCP
                             |
                             v
                 ┌───────────────────────┐
                 │      RETRIEVAL MCP   │
                 │        "HOW?"         │
                 │                       │
                 │ vector                │
                 │ keyword              │
                 │ hybrid               │
                 │ rerank               │
                 └───────────┬───────────┘
                             |
                             v
                          EVIDENCE
                             |
                            A2A
                             |
                             v
                 ┌───────────────────────┐
                 │     SYNTHESIS AGENT   │
                 │      "ANSWER?"        │
                 └───────────────────────┘
```

That separation gives you a system where you can independently replace the parser, vector database, retrieval algorithm, embedding model, LLM, or agent without redesigning everything.
