---
name: sql-database-safety
description: The procedure for reading from and writing to the database safely — discover the schema before writing SQL, preview what a mutation will affect before running it, bind values as parameters instead of interpolating them, and get explicit confirmation before anything destructive. Use this before any db_query_data, db_insert_data, db_update_data, db_delete_data, db_create_table or db_update_table_definition call.
compatibility: Requires the db MCP server (backend/mcp_servers/db) against PostgreSQL. Full-table deletes and destructive schema operations are disabled at the server by default and will be rejected regardless of call flags.
---

# Database procedure

The server never trusts your SQL: mutation tools build their own parameterized statements from validated input, and `db_query_data` (the only raw-SQL tool) is AST-validated and read-only. That protects the *database*. It does not protect the *user* from a correct-but-wrong query hitting the wrong rows — that's what this procedure is for.

## Step 1 — Discover. Never guess a name.

1. `db_list_tables` — what exists, and which operations you're permitted per table. A table you don't see for an operation is one you may not perform it on.
2. `db_describe_table(table)` — real column names, logical types (`STRING`, `INTEGER`, `FLOAT`, `BOOLEAN`, `DATETIME`), nullability, defaults, primary key.

Required before any write to a table you didn't create yourself in this same turn, and before any non-trivial query. Guessing a column name costs a failed call and a visible stumble; two discovery calls cost less.

## Step 2 — Read with `db_query_data`, and always bind values

```
sql="SELECT id, title FROM documents WHERE source = %s AND created_at > %s LIMIT 20"
parameters=["internal", "2026-01-01"]
```

Never build the value into the SQL string yourself (no f-string, `.format()`, or concatenation of a value into `sql`). Placeholders + `parameters` is the injection defense; the AST validator checks query *shape*, not the literal values inside it, so an interpolated value slips straight through a structurally-valid SELECT.

`parameters` must have exactly as many entries as there are `%s`, in order.

Hard limits — a query that exceeds one is rejected or truncated, not silently trimmed to something wrong:

| Limit | Value |
|---|---|
| Rows returned | 1,000 |
| Statement timeout | 10s |
| SQL text length | 10,000 chars |
| Tables per query / joins | 10 / 8 |
| CTEs / subquery depth | 5 / 4 |

`db_query_data` is read-only by construction. INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/GRANT/REVOKE, multiple statements and transaction control are all rejected — use the structured tools below instead of trying to phrase a mutation as SQL.

## Step 3 — Preview before you mutate

Before `db_update_data` or `db_delete_data`, run the **same filter as a SELECT** and look at what it matches:

```
SELECT count(*) FROM orders WHERE status = %s     -- parameters: ["cancelled"]
```

Then tell the user the real number before acting: *"That matches 412 rows in `orders`. Delete them?"* This is the single most valuable habit in this skill — it converts an irreversible surprise into a decision the user actually made. A filter that matches 0 rows, or 10× more than expected, almost always means you misunderstood the request.

## Step 4 — Write with the structured tools

| Goal | Tool | Notes |
|---|---|---|
| New table | `db_create_table` | Logical type names only (`STRING`/`INTEGER`/`FLOAT`/`BOOLEAN`/`DATETIME`) — not `TEXT`/`VARCHAR`/`INT`. `if_exists="fail"` by default, which is usually what you want |
| Change shape | `db_update_table_definition` | Atomic: if one operation fails, none are kept |
| Add rows | `db_insert_data` | Batch — every record must share the same columns. Max 1,000 per call |
| Change rows | `db_update_data` | `filter` required. Supports `$eq $ne $gt $gte $lt $lte $in $is_null` |
| Remove rows | `db_delete_data` | `filter` required |

Values are always bound for you by these tools; you never write SQL text for them.

## Step 5 — Destructive operations: confirm, and know what's disabled

**Ask first, in plain language, and wait for a clear yes.** State the table, the operation, and the real row count from Step 3. Never infer consent from a vague instruction like "clean that up" — ask what specifically should go.

Two things are **disabled at the server by default** and will be rejected no matter what flags you pass:

- **Full-table delete** (`db_delete_data` without a filter, even with `allow_full_table_delete: true` + `confirm: true`).
- **Destructive schema ops** (`drop_column`, `alter_column_type`, `drop_constraint`, `drop_index` — even with `confirm: true` on the operation).

If one of these is what the user wants, say plainly that it's disabled by server configuration and an operator must enable it. Do **not** retry with different flags, and do **not** engineer around it (e.g. deleting every row via a always-true filter to simulate a full-table delete, or recreating a table to drop a column). Working around a safety switch defeats the reason it exists.

## Step 6 — Verify and report honestly

- Check `truncated` on any read. If true, you are looking at a partial result — narrow the query rather than telling the user "that's all of them."
- After a write, report the actual `affected_rows`/`inserted_count`/`deleted_count` the tool returned, not what you intended to happen. A mutation that reports 0 affected rows did nothing — investigate the filter instead of reporting success.

## Self-check before any write

- [ ] I ran `db_describe_table` (or created this table myself this turn).
- [ ] I previewed the filter as a SELECT and know the real row count.
- [ ] I told the user that count and got an explicit yes, if this is destructive.
- [ ] Every value is bound via `parameters`, not interpolated.
- [ ] I am not trying to route around a disabled safety switch.

## Anti-patterns

| Don't | Why |
|---|---|
| Interpolate a value into `sql` | Bypasses binding — the injection risk this tool was built to prevent |
| Mutate without previewing the filter | Turns a misunderstanding into irreversible data loss |
| Retry a rejected destructive op with different flags | It's a server master switch, not a call-level argument |
| Simulate a full-table delete with `WHERE 1=1` | Deliberately defeating a safety control |
| Report success on 0 affected rows | The filter was wrong; the user's request wasn't done |
| Claim a truncated result is complete | Silently wrong answers |
