-- Agent tasks (Deep Research, Data Analyst, and any agent added later):
-- one row per task, plus an append-only event log the UI replays/streams.
CREATE TABLE app.agent_tasks (
    id             text PRIMARY KEY,
    user_id        text NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    agent_id       text NOT NULL,
    title          text NOT NULL,
    status         text NOT NULL,          -- submitted | working | input_required | completed | failed | canceled
    inputs         jsonb NOT NULL DEFAULT '{}'::jsonb,
    a2a_task_id    text,
    a2a_context_id text,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_tasks_user_idx ON app.agent_tasks (user_id, created_at DESC);

CREATE TABLE app.agent_task_events (
    seq        bigserial PRIMARY KEY,
    task_id    text NOT NULL REFERENCES app.agent_tasks(id) ON DELETE CASCADE,
    kind       text NOT NULL,             -- status | step | question | answer | artifact | error
    data       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_task_events_task_idx ON app.agent_task_events (task_id, seq);
