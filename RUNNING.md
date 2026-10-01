# Running this app

There are two ways to run the Agentic API + its 4 MCP servers (indexing,
retrieval, db, code). Both read config from the same `backend/.env` file
(see `backend/.env.example` for the full list of keys). Pick one — they are
not meant to run at the same time (both want ports 8000-8003/8100/5432).

Either way there is **one database**: Postgres with the pgvector extension
(`pgvector/pgvector:pg16`). It holds users, chats, the document registry,
the document vectors, and each user's private DB-tool tables. MongoDB and
Chroma are gone — see "Upgrading from MongoDB/Chroma" below if you have
data in them.

## Option A — separate processes (`start_mcp_servers.ps1`)

The original local-dev setup: each MCP server runs as its own long-lived
HTTP service in its own console window, plus Postgres in a Docker
container. This is the
fastest loop for developing a single server in isolation (e.g. iterating on
`indexing/server.py` with a debugger attached to just that process), and
matches how `nginx.conf` expects things to be laid out if you use nginx as a
reverse proxy locally.

```powershell
cd C:\Users\Nigia\source\repos\AgenticApp
.\start_mcp_servers.ps1
```

This script starts, in order: Postgres (Docker, pgvector image — an old
`postgres:16` container is recreated on the new image, keeping its data
volume), `python -m backend.scripts.db_setup` (migrations + the DB server's
restricted role), the indexing/retrieval/db MCP servers (ports 8000-8002),
the code sandbox as a locked-down container (`agenticapp-code-sandbox`,
published on `127.0.0.1:8003`), then the Agentic API (port 8100) once all 4
are up. Unlike the compose setup, Docker Desktop can't cut the sandbox off
from the internet or from ports on your PC (`host.docker.internal`), so set
a real Postgres password before letting anyone else use a ps1-run app. `backend/apps/client/servers.json`
points the API at `http://localhost:800{0,1,2,3}/mcp` for this mode — leave
it as-is; nothing about this mode changed.

Requires: Docker Desktop running (for Postgres), and nginx if you want the
reverse-proxy step.

## Option B — one container (Docker)

The deploy-friendly path: the API and all 4 MCP servers run as a single
process tree inside one container (the MCP servers are launched as stdio
subprocesses of the API instead of separate HTTP services — see
`backend/apps/client/servers.docker.json` and each server's
`MCP_TRANSPORT=stdio` handling in its `__main__` block). Postgres runs as
its own container.

```bash
docker compose up --build
```

This brings up 3 containers: `app` (API + the indexing/retrieval/db MCP
servers, port 8100), `code-sandbox` (the code MCP server, locked down — see
`backend/mcp_servers/code/README.md`; reachable only from `app`, no internet)
and `postgres` (pgvector image; reachable from the host on `127.0.0.1:5432`
only). `docker-compose.yml` reuses `backend/.env` for everything (API keys,
model names, feature flags, ...) and only overrides the database URLs to
point at the `postgres` service. Passwords come from a root `.env` file
(`POSTGRES_PASSWORD`, `AGENT_DB_PASSWORD`); the defaults are for local use
only. Note: Postgres only reads `POSTGRES_PASSWORD` when it creates a fresh
data volume — changing it later means changing the password inside the
database too.

This is also the shape a free-tier host (Render/Fly.io/Railway) expects: one
`Dockerfile`, one exposed port. For an actual deploy you can point the
same env vars at a managed Postgres instead, as long as it offers the
`vector` extension.

Requires: Docker (no local Python/venv install needed — it's all
inside the image).

## Either way

- Before running for the first time, copy `backend/.env.example` to
  `backend/.env` and fill in real values (`OPENAI_API_KEY` at minimum if
  `ENVIRONMENT=PROD`). New since the move to Postgres: `DATABASE_URL`,
  `DATABASE_AGENT_ROLE_PASSWORD` and `DB_MCP_DATABASE_URL` (the DB tool's
  restricted role — its password must match `DATABASE_AGENT_ROLE_PASSWORD`).
- `AUTH_DEV_BYPASS=true` in `.env` treats every request as one hardcoded dev
  user — fine for solo local use, must be `false` before anyone else can
  reach the deployment.
- Health check once running: `GET http://localhost:8100/api/health`.

## Upgrading from MongoDB/Chroma

Conversations, accounts and the document list used to live in MongoDB, and
document vectors in Chroma. To bring existing data across (once):

1. Start the old Mongo container next to the new stack, e.g.
   `docker start agenticapp-mongo` (ps1 setup) — or for the compose setup
   `docker run -d --name old-mongo -p 27017:27017 -v agenticapp_agenticapp_mongodata:/data/db mongo:7`
   (check `docker volume ls` for the exact volume name).
2. Copy accounts, conversations and the document list:
   `pip install pymongo` then
   `python -m backend.scripts.migrate_mongo_to_pg --mongo-url mongodb://localhost:27017 --mongo-db agentic_chat`
   (inside Docker: `docker compose exec app ...`). Passwords carry over;
   everyone signs in once more.
3. Rebuild the vector index from the original files in `data/documents`
   (Chroma's vectors don't carry over, the files do):
   `python -m backend.scripts.reindex_all`
4. Remove the old Mongo/Chroma containers and volumes once you're happy.

Agent-created DB tables from before this change stay in the old `public`
schema and aren't visible to anyone any more (every user now has a private
workspace). Copy any you still need into a user's workspace by hand.

## Agents (Deep Research, Data Analyst, ...)

A separate **agents service** (`python -m backend.apps.agents.main`, port
9100) runs every registered A2A agent; the API drives it and the frontend's
**Agents** page (`/agents`) shows any agent it lists. `docker compose up`
starts it as the `agents` container; `start_mcp_servers.ps1` starts it as
its own window. Both the API and the agents service must have the same
`AGENTS_INTERNAL_TOKEN` in `backend/.env` — agents stay disabled until it's
set. Agents never talk to the MCP servers directly: they call tools through
the API (`/internal/tools`), so the chat's ownership checks, approvals and
sandbox apply to them too.

**Adding an agent:** write one `BaseAgent` subclass (id, name, description,
`inputs` form fields, `allowed_tools`, and an `async run(ctx, inputs)` that
uses `ctx.step`, `ctx.ask`, `ctx.call_tool`/`ctx.tool_loop` and
`ctx.artifact`) and add it to `backend/apps/agents/registry.py`. The A2A
card, the API endpoints and the UI form pick it up automatically — see the
docstring at the top of `backend/apps/agents/base.py` and the two examples
(`research.py`, `analyst.py`).
