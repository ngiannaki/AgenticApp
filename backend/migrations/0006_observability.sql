-- Usage + latency + cost tracking (backend/core/observability.py).
-- Metadata only: no prompts, answers or tool arguments are stored.

-- One row per LLM call (chat, agents).
CREATE TABLE app.llm_calls (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    user_id         text NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    trace_id        text,
    conversation_id text,
    task_id         text,
    feature         text NOT NULL,            -- 'chat' | 'agent:<agent id>'
    model           text NOT NULL,
    input_tokens    integer NOT NULL DEFAULT 0,
    cached_tokens   integer NOT NULL DEFAULT 0,
    output_tokens   integer NOT NULL DEFAULT 0,
    reasoning_tokens integer NOT NULL DEFAULT 0,
    latency_ms      integer,
    ttft_ms         integer,                  -- time to first token (streaming only)
    cost_usd        double precision,         -- NULL = model has no known price
    estimated       boolean NOT NULL DEFAULT false,  -- tokens estimated, not reported by the server
    status          text NOT NULL DEFAULT 'ok',
    error           text
);
CREATE INDEX llm_calls_user_ts_idx ON app.llm_calls (user_id, ts DESC);
CREATE INDEX llm_calls_ts_idx ON app.llm_calls (ts);
CREATE INDEX llm_calls_trace_idx ON app.llm_calls (trace_id);

-- One row per tool call (MCP tools), with per-stage timings for retrieval.
CREATE TABLE app.trace_spans (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    user_id     text NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    trace_id    text,
    feature     text NOT NULL,
    kind        text NOT NULL,                -- 'tool'
    name        text NOT NULL,
    duration_ms integer NOT NULL,
    status      text NOT NULL DEFAULT 'ok',   -- ok | error | timeout
    attrs       jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX trace_spans_user_ts_idx ON app.trace_spans (user_id, ts DESC);
CREATE INDEX trace_spans_ts_idx ON app.trace_spans (ts);
CREATE INDEX trace_spans_trace_idx ON app.trace_spans (trace_id);
