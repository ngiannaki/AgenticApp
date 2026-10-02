-- User-built agents and the reusable skills they are made of
-- (backend/apps/api/custom_agents.py). A skill = instructions + the tool
-- groups it may use (backend/core/agent_tools.py); an agent = instructions +
-- a list of skill ids. Both private to their owner.
CREATE TABLE app.agent_skills (
    id           text PRIMARY KEY,
    user_id      text NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    name         text NOT NULL,
    description  text NOT NULL DEFAULT '',
    instructions text NOT NULL DEFAULT '',
    tools        jsonb NOT NULL DEFAULT '[]'::jsonb,   -- tool group keys
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE TABLE app.custom_agents (
    id           text PRIMARY KEY,
    user_id      text NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    name         text NOT NULL,
    description  text NOT NULL DEFAULT '',
    instructions text NOT NULL DEFAULT '',
    skill_ids    jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);
