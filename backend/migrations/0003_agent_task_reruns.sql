-- A rerun of a task (with the user's feedback) links back to the original.
ALTER TABLE app.agent_tasks ADD COLUMN parent_task_id text REFERENCES app.agent_tasks(id) ON DELETE SET NULL;
