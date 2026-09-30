# Running this app

There are two ways to run the Agentic API + its 4 MCP servers (indexing,
retrieval, db, code). Both read config from the same `backend/.env` file
(see `backend/.env.example` for the full list of keys). Pick one — they are
not meant to run at the same time (both want ports 8000-8003/8100/27017/5432).

## Option A — separate processes (`start_mcp_servers.ps1`)

The original local-dev setup: each MCP server runs as its own long-lived
HTTP service in its own console window, plus Mongo/Postgres in Docker
containers and Chroma as a standalone `chroma.exe` process. This is the
fastest loop for developing a single server in isolation (e.g. iterating on
`indexing/server.py` with a debugger attached to just that process), and
matches how `nginx.conf` expects things to be laid out if you use nginx as a
reverse proxy locally.

```powershell
cd C:\Users\Nigia\source\repos\AgenticApp
.\start_mcp_servers.ps1
```

This script starts, in order: Postgres (Docker), MongoDB (Docker), Chroma
(`chroma.exe`, port 8010), the 4 MCP servers (ports 8000-8003), then the
Agentic API (port 8100) once all 4 report healthy. `backend/apps/client/servers.json`
points the API at `http://localhost:800{0,1,2,3}/mcp` for this mode — leave
it as-is; nothing about this mode changed.

Requires: Docker Desktop running (for Postgres/Mongo), Chroma installed in
the venv (`chroma.exe` alongside `python.exe`), and nginx if you want the
reverse-proxy step.

## Option B — one container (Docker)

The deploy-friendly path: the API and all 4 MCP servers run as a single
process tree inside one container (the MCP servers are launched as stdio
subprocesses of the API instead of separate HTTP services — see
`backend/apps/client/servers.docker.json` and each server's
`MCP_TRANSPORT=stdio` handling in its `__main__` block). Mongo, Postgres,
and Chroma still run as their own containers.

```bash
docker compose up --build
```

This brings up 4 containers: `app` (API + 4 MCP servers, port 8100), `mongo`
(27017), `postgres` (5432, auto-runs `0001_init.sql` on first start), and
`chroma` (8010 on the host, 8000 internally). `docker-compose.yml` reuses
`backend/.env` for everything (API keys, model names, feature flags, ...)
and only overrides `API_MONGO_URL` / `DB_MCP_DATABASE_URL` / `VECTOR_STORE_HOST`
/ `VECTOR_STORE_PORT` to point at the compose service names instead of
`localhost`.

This is also the shape a free-tier host (Render/Fly.io/Railway) expects: one
`Dockerfile`, one exposed port. For an actual deploy, swap the `mongo` /
`postgres` / `chroma` services for managed free instances (MongoDB Atlas,
Neon/Supabase Postgres, a small persistent-disk Chroma or a self-hosted one)
and point the same env vars at them instead of running `docker-compose.yml`'s
local containers.

Requires: Docker (no local Python/venv/Chroma install needed — it's all
inside the image).

## Either way

- Before running for the first time, copy `backend/.env.example` to
  `backend/.env` and fill in real values (`OPENAI_API_KEY` at minimum if
  `ENVIRONMENT=PROD`).
- `AUTH_DEV_BYPASS=true` in `.env` treats every request as one hardcoded dev
  user — fine for solo local use, must be `false` before anyone else can
  reach the deployment.
- Health check once running: `GET http://localhost:8100/api/health`.
