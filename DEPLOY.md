# Deploying for a small team (~10 people)

Two supported setups, both built on `docker compose` (see RUNNING.md for the
plain local run). Nothing here is required for local use.

| | VM with a domain | Your PC + Cloudflare tunnel |
|---|---|---|
| Command | `docker compose --profile edge up --build -d` | `docker compose --profile tunnel up --build -d` |
| HTTPS | Caddy (automatic Let's Encrypt) | Cloudflare |
| Open ports | 80, 443 | none |

In both, the `web` service (Caddy) serves the built frontend at `/` and
proxies `/api/*` to the API. `/internal/*`, the MCP servers, the agents and
Postgres are never reachable from outside.

## 1. Prepare (both)

1. Clone both repos side by side: `AgenticApp/` and `agenticapp-frontend/`
   (or set `FRONTEND_DIR` in the root `.env`).
2. `cp backend/.env.example backend/.env` and set at least:
   - `ENVIRONMENT=PROD`, `OPENAI_API_KEY` (and the model names you use)
   - `AUTH_DEV_BYPASS=false`
   - `MCP_INTERNAL_TOKEN`, `AGENTS_INTERNAL_TOKEN`:
     long random strings — `python -c "import secrets; print(secrets.token_urlsafe(32))"`
   - `AUTH_INVITE_CODE`: what people type to sign up (blank = signup off;
     create accounts with `docker compose exec app python -m backend.scripts.create_user`)
   - `API_CORS_ORIGINS`: your public URL (same origin, so mostly a safeguard)
3. `cp .env.example .env` and set `POSTGRES_PASSWORD`, `AGENT_DB_PASSWORD`,
   the same `MCP_INTERNAL_TOKEN`, and the edge settings below.

## 2a. VM with a domain (profile `edge`)

- A small VM (2 vCPU / 8 GB RAM is comfortable — Docling and the reranker
  are the heavy parts), Docker installed, ports 80/443 open.
- DNS: an `A` record for e.g. `app.example.com` pointing at the VM.
- Root `.env`: `SITE_ADDRESS=app.example.com`.
- `docker compose --profile edge up --build -d` — Caddy gets the certificate
  on first request.

## 2b. Your PC + Cloudflare named tunnel (profile `tunnel`)

- In the Cloudflare dashboard: Zero Trust → Networks → Tunnels → create a
  tunnel (type cloudflared), add a public hostname (e.g.
  `app.example.com`) with service `http://web:80`. Copy the tunnel token.
- Root `.env`: `CLOUDFLARE_TUNNEL_TOKEN=<token>`, `SITE_ADDRESS=:80`.
- `docker compose --profile tunnel up --build -d`. No ports need to be
  opened on your router; the PC must stay on.
- Optional: put Cloudflare Access (email one-time PIN) in front as a second
  gate.

## 3. Operations

- **Health:** `docker compose ps` (the `app` healthcheck probes MCP + Postgres).
- **Logs:** `docker compose logs -f app` (plus `./logs/`).
- **Update:** `git pull` in both repos, then the same `up --build -d` command.
  Migrations run automatically (`db-setup`).
- **Backups** (daily cron on the host):
  ```bash
  docker compose exec -T postgres pg_dump -U raguser -Fc ragdb > backup-$(date +%F).dump
  tar czf data-$(date +%F).tgz data/      # uploaded files + artifacts
  ```
  Restore: `docker compose exec -T postgres pg_restore -U raguser -d ragdb --clean < backup.dump`.
- **Limits:** per-user daily quotas (`API_DAILY_*` in backend/.env) keep one
  person from spending the whole OpenAI budget.

## Checklist before sharing the link

- [ ] `AUTH_DEV_BYPASS=false`, an invite code set (or signup off)
- [ ] Both internal tokens and both DB passwords changed from defaults
- [ ] `docker compose ps` shows only `web` (and `cloudflared`) with public ports
- [ ] Sign up as a second user and check you can't see the first user's documents
