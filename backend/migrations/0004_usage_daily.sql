-- Per-user daily counters for the quotas in backend/apps/api/quotas.py
-- (chat turns, uploads, agent tasks).
CREATE TABLE app.usage_daily (
    user_id text NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    day     date NOT NULL DEFAULT current_date,
    kind    text NOT NULL,
    count   integer NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, kind)
);
