# AgenticApp

A multi-user assistant over your own documents and data: chat with cited
answers, a private SQL workspace, a sandboxed Python runner, and A2A agents
(Deep Research, Data Analyst, Extractor) for longer jobs.

- **API** — FastAPI + SSE (`backend/apps/api`), one Postgres with pgvector
- **MCP servers** — indexing, retrieval, db, code (`backend/mcp_servers`)
- **Agents** — A2A service (`backend/apps/agents`); add one by subclassing
  `BaseAgent` and listing it in `registry.py`
- **Frontend** — Next.js static export, in the `AgenticApp-frontend` repo

| Doc | What |
|---|---|
| [RUNNING.md](RUNNING.md) | Run locally: `start_mcp_servers.ps1` or `docker compose up --build` |
| [DEPLOY.md](DEPLOY.md) | Share it with a team: VM + Caddy, or your PC + Cloudflare tunnel |
| [history/](history/) | Change log and to-do list |
| [docs/archive/](docs/archive/) | Older notes (some describe the pre-Postgres setup) |

Tests: `TEST_DATABASE_ADMIN_URL=postgresql://postgres@localhost:5432/postgres pytest backend/tests`
