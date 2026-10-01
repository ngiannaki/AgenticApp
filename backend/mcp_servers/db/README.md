# Database MCP Server

A FastMCP server that gives an AI agent controlled access to a PostgreSQL
database: schema management, data mutations, and read-only SQL querying.
Part of this repo's RAG stack, alongside the `indexing` and `retrieval` MCP
servers in `backend/mcp_servers/`.

## Why this isn't "give the agent a SQL connection"

The agent's stated intent is never the security boundary. Five of the six
tools never see SQL at all — they take structured input and generate
parameterized SQL internally. The sixth (`query_data`) does accept raw SQL,
but every query is parsed into a real AST (via `sqlglot`, Postgres dialect)
and run through a deterministic validation pipeline before it touches a
connection; a prompt injection or a hallucinated tool call cannot talk its
way past that pipeline the way it might talk its way past an LLM-based
"is this query safe?" check.

## Per-user workspaces

Every user gets a private Postgres schema, `ws_<hash of their user id>`
(see `database/workspace.py`). Every tool takes a `user_id` — always injected
by the API from the signed-in session, never chosen by the model — and the
whole call runs inside that user's schema: introspection only looks there,
writes run with `search_path` pinned to it, and `query_data` rejects any
schema-qualified name. One user can never see or touch another user's
tables.

The server connects as its own restricted role (`DATABASE_AGENT_ROLE`,
default `agent_ws`) which has no access to the app's own `app` (users,
chats) and `rag` (document vectors) schemas — even a bug in the validator
couldn't read them. The API creates that role at startup from
`DATABASE_AGENT_ROLE_PASSWORD`.

## Setup

Normally nothing to do: `docker compose up` or `start_mcp_servers.ps1` start
Postgres (pgvector image), run the migrations and create the role. By hand:

1. **Install dependencies**: `pip install -r backend/mcp_servers/db/requirements.txt`
2. **Prepare the database**: `python -m backend.scripts.db_setup` (reads
   `DATABASE_URL` / `DATABASE_AGENT_ROLE_PASSWORD` from `backend/.env`).
3. **Point this server at the restricted role**: `DB_MCP_DATABASE_URL=postgresql://agent_ws:<password>@localhost:5432/ragdb`
4. **Permissions** (`permissions.yaml`): inside a user's own workspace
   everything is allowed by default; writes are still gated by the API's
   mandatory human approval (`API_MANDATORY_APPROVAL_TOOLS`). See
   `permissions.example.yaml` for per-table rules.
5. **Run the server** (matches how `indexing`/`retrieval` are started; see
   the repo's `start_mcp_servers.ps1` / `RUNNING.md`):
   ```
   .venv/Scripts/python.exe backend/mcp_servers/db/server.py
   ```
   It listens on `http://0.0.0.0:8002/mcp` by default (`DB_MCP_PORT`).
6. **Point a client at it.** `backend/apps/client/servers.json` already
   registers `"db": {"url": "http://localhost:8002/mcp"}`; the generic
   `example_mcp_client.py` will pick it up (tools appear namespaced as
   `db_create_table`, `db_query_data`, etc.) once you start the server and
   update its system prompt to mention the new tools.

## The six tools

| Tool | Accepts | Purpose |
|---|---|---|
| `create_table` | structured columns/indexes | Generates `CREATE TABLE` |
| `update_table_definition` | structured operations | Generates `ALTER TABLE` / index DDL |
| `insert_data` | records (dicts) | Parameterized `INSERT` (batch) |
| `update_data` | filter + changes (dicts) | Parameterized `UPDATE` |
| `delete_data` | filter (dict) | Parameterized `DELETE` |
| `query_data` | raw SQL + params | Validated, read-only `SELECT` |

`create_table`/`update_table_definition` accept a deliberately small, closed
column-type vocabulary: `string`, `integer`, `float`, `boolean`, `datetime`
(mapped internally to `TEXT`, `INTEGER`, `DOUBLE PRECISION`, `BOOLEAN`,
`TIMESTAMP`). This is not the full set of types Postgres supports — that's
intentional; see `ALLOWED_COLUMN_TYPES` in `models/schema.py`.

`update_data`/`delete_data` filters use a small Mongo-ish operator set:
`$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte`, `$in`, `$is_null` — or a plain
scalar for implicit equality.

`query_data` takes psycopg-style `%s` placeholders (this server uses
synchronous `psycopg`, not `asyncpg`, so parameters are `%s`, not `$1`):
```json
{"sql": "SELECT id, title FROM documents WHERE source = %s LIMIT 20", "parameters": ["internal"]}
```

## query_data's validation pipeline

```
length check -> parse (sqlglot, postgres dialect) -> single-statement check
-> statement-type check (SELECT / WITH+SELECT only)
-> banned-construct scan (INSERT/UPDATE/DELETE/DDL/GRANT/COPY/locks/
   transaction control — checked over the *whole* tree, including inside
   CTEs, because Postgres allows a data-modifying statement inside a CTE
   of an overall SELECT)
-> dangerous-function denylist (pg_sleep, pg_read_file, dblink, lo_*,
   pg_terminate_backend, set_config, xpath, ...)
-> table authorization (no schema-qualified names, table must exist in the
   caller's own workspace, must have
   'select' permission, table count under the configured limit)
-> column authorization (per-table deny_columns; SELECT * blocked when a
   referenced table has any denied columns, since * can't be filtered)
-> complexity limits (join count, CTE count, subquery/CTE nesting depth)
-> wrap in `SELECT * FROM (<query>) AS _sub LIMIT <max_rows + 1>` and execute
-> row/byte caps enforced again during result streaming (defense in depth)
-> `truncated: true` in the response if the cap was hit
```

Nothing here is best-effort sanitization — every stage is a hard allow/deny,
and a query that fails any stage is rejected with a structured error rather
than partially executed.

## Authorization model

`permissions.yaml` is deny-by-default: a table not listed there has no
permissions at all, unless a `default` policy is set — in which case any
unlisted, non-protected table falls back to it. This is what lets the agent
create and use new tables (`db_create_table`) without every table name being
pre-registered. Each listed table has independent `select` / `insert` /
`update` / `delete` / `schema_change` flags plus an optional `deny_columns`
list. `protected_tables` is a second, orthogonal layer for RAG
infrastructure tables (chunks, embeddings, document_versions, ...): being
protected hard-blocks insert/update/delete/schema_change *regardless* of the
per-table flags — a config typo that accidentally grants `delete: true` on a
protected table still can't be used to delete from it.

Server-wide switches in `.env` (`DB_MCP_ALLOW_SCHEMA_CHANGES`,
`DB_MCP_ALLOW_DELETE`, `DB_MCP_ALLOW_FULL_TABLE_DELETE`,
`DB_MCP_ALLOW_DESTRUCTIVE_SCHEMA_OPS`) are a second gate on top of the
per-table permissions — both must agree for the more dangerous operations
to be reachable at all.

## Guardrails against accidental mass mutation

- `update_data` requires a `filter` unless the caller explicitly passes
  `allow_full_table_update: true`.
- `delete_data` requires a `filter` unless the caller passes
  `allow_full_table_delete: true` **and** `confirm: true` **and** the server
  has `DB_MCP_ALLOW_FULL_TABLE_DELETE=true` — three independent opt-ins.
- `update_table_definition`'s destructive operations (`drop_column`,
  `alter_column_type`, `drop_constraint`, `drop_index`) require
  `confirm: true` on the operation **and**
  `DB_MCP_ALLOW_DESTRUCTIVE_SCHEMA_OPS=true` on the server.
- `create_table` never overwrites an existing table; `if_exists` is
  explicit (`"fail"` or `"ignore"`), there is no silent replace.

## Architectural decisions worth calling out

- **Sync `psycopg` 3, not `asyncpg`.** Nothing async exists anywhere else in
  this backend (both sibling MCP servers are plain synchronous
  `@mcp.tool` functions on top of sync ChromaDB/LlamaIndex calls), and
  FastMCP tools here don't need to be async to be concurrent — the
  connection pool (`psycopg_pool.ConnectionPool`) handles concurrent callers.
  This also means the raw-SQL parameter style is `%s` (psycopg's paramstyle),
  not `$1`; see `query_data` above.
- **`sqlglot` for parsing, not `sqlparse`.** `sqlparse` is a tokenizer, not
  an AST — checking "does this contain a DROP" against tokens is exactly the
  string-matching approach the brief calls out as insufficient. `sqlglot`
  gives a real expression tree (`exp.Insert`, `exp.Table`, `exp.Column`,
  `exp.Join`, ...) that can be walked structurally, including into CTEs.
- **Deny-by-default authorization, config-driven, not code-driven.**
  `protected_tables` and per-table CRUD flags live in YAML specifically so
  RAG-infrastructure table names are never hard-coded into `sql_validator.py`
  or `permissions.py` — a new protected table is a one-line config change.
- **A schema/columns cache (`Database.get_columns`) backed by
  `information_schema`, not just the permissions file.** Permissions say
  what's *allowed*; `information_schema` says what *exists*. Both are
  checked — a permissions entry for a typo'd table name doesn't create a
  false sense of an existing, safe table.
- **`query_data` always wraps the validated query in an outer
  `SELECT * FROM (...) LIMIT n` rather than rewriting the caller's SQL AST.**
  Rewriting the AST and re-serializing it risks subtly changing semantics
  (or breaking `%s` placeholder positions during re-emission); wrapping the
  original text is simpler and provably safe as an additive cap.
- **Every write path uses `transactions.transaction()`** (`SET LOCAL
  statement_timeout` + commit/rollback), so a batch insert or a multi-op
  `update_table_definition` call is atomic — a failure partway through never
  leaves a half-applied change.
- **No ORM.** SQLAlchemy is installed in the shared venv as someone else's
  transitive dependency but isn't used anywhere in this backend; introducing
  it here for a handful of hand-controlled DDL/DML shapes would add a large
  abstraction layer for something this module already generates directly
  and more legibly with parameterized psycopg calls.

## Error categories

Every tool returns `{"success": false, "error": "...", "error_category": "..."}`
on failure, where `error_category` is one of `validation_error`,
`authorization_error`, `query_error`, `constraint_error`, `timeout_error`,
`unsupported_operation`, `database_error`. Messages are written to be safe
to show an agent: no connection strings, file paths, stack traces, or raw
database exception text.

## Logging

Structured logs go to `logs/db_server.log` (same convention as the other
MCP servers). Each call logs a short request id, tool name, table (when
applicable), elapsed time, and row/affected counts on success, or the error
category on failure. Full record payloads and raw SQL text are not logged.
