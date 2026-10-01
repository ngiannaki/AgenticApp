# Docker Runbook

Operational reference for running, restarting, and cleaning up this
project's Docker stack. For the two ways to run the app at all (separate
processes vs. one container), see `RUNNING.md` — this doc assumes you're
using Option B (`docker compose`).

## The stack

`docker-compose.yml` defines 4 services, all under the compose project name
`agenticapp` (the repo folder name), so container names are
`agenticapp-<service>-1`:

| Service    | Image                   | Port(s)        | Volume                  |
|------------|-------------------------|-----------------|--------------------------|
| `app`      | built from `Dockerfile` | 8100            | —                        |
| `mongo`    | `mongo:7`               | 27017           | `agenticapp_mongodata`   |
| `postgres` | `postgres:16`           | 5432            | `agenticapp_pgdata`      |
| `chroma`   | `chromadb/chroma:latest`| 8010 (→8000 in) | `./data/vector_store`    |

Only ever manage these through `docker compose`, not `docker run` — a
manual `docker run --name agenticapp-mongo ...` creates a container that
*looks* like it belongs to this project but isn't tracked by compose, so it
won't stop/start with the rest of the stack and just lingers (see
"Diagnosing stray containers" below — this has already happened once).

## Starting from scratch

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f app
```

`--build` rebuilds the `app` image from the current `Dockerfile`/`backend/`
source; drop it for a pure restart with no code changes. Health check once
`app` is up: `GET http://localhost:8100/api/health`.

## Picking up code changes during development

`app`'s `backend/` source is bind-mounted into the container
(`./backend:/app/backend` in `docker-compose.yml`), so edits on the host
are visible inside the container immediately. There is **no file watcher
and no auto-reload on save** — on purpose, so nothing restarts until you
decide it should. When you're ready for the running app to pick up your
changes, trigger it yourself:

```bash
docker compose restart app
```

This restarts just the `app` container's process — re-running the whole
FastAPI lifespan, including reconnecting to Mongo and respawning the 4 MCP
stdio subprocesses — without rebuilding the image and without touching
`mongo`/`postgres`/`chroma`. Since the code is mounted rather than baked
in, the restarted process picks up whatever is currently on disk under
`backend/`, no `--build` needed.

This only covers `backend/` edits. Changing `requirements*.txt`, the
`Dockerfile` itself, or `docker-compose.yml` still needs
`docker compose up --build -d` — the mount only overlays the source tree,
not the image's installed dependencies. The Dockerfile's own `CMD` (used
for real deploys, not this compose file) is untouched and still bakes the
code in, which is what you want for a deploy image.

## Restarting cleanly (closing out old processes first)

`docker compose restart app` above is for the common case — applying a
code edit to an already-healthy stack. For an actual reset (something's
gone wrong, state looks off, or you suspect stray/orphaned containers),
resending signals to the existing containers isn't enough — any drift
survives a plain `restart`. Tear down and bring back up instead:

```bash
docker compose down --remove-orphans -t 30
docker compose up --build -d
```

- `--remove-orphans` also removes any container under this project name
  that compose no longer recognizes from the current `docker-compose.yml`
  (e.g. leftover stray containers from a prior manual `docker run`).
- `-t 30` gives containers 30s to shut down gracefully (SIGTERM) before
  Docker gives up and sends SIGKILL. The default is 10s — if `app` doesn't
  shut down its MCP stdio subprocesses / DB connections within that window
  it gets force-killed, which shows up as **exit code 137** in
  `docker compose ps -a`. A 137 on `app` is a symptom worth checking the
  logs for (something not releasing cleanly on shutdown), not just noise —
  don't treat it as routine.
- `down` (without `-v`) does **not** touch named volumes, so `mongo`/
  `postgres` data in `agenticapp_mongodata`/`agenticapp_pgdata` survives a
  normal restart. Only `docker compose down -v` would wipe them.

## Diagnosing stray containers

`docker ps -a` shows *every* container on the machine, including ones
unrelated to this project. To see only what compose actually manages here:

```bash
docker compose ps -a
```

If a container's name looks like it belongs to this project (e.g.
`agenticapp-mongo` with no `-1` suffix) but doesn't show up in
`docker compose ps -a`, it's an orphan — started outside compose at some
point. `docker compose down --remove-orphans` cleans these up; they won't
reappear as long as `mongo`/`postgres`/`chroma` are only ever started via
`docker compose up`.

## Housekeeping

Rebuilding `app` repeatedly leaves old, now-unreferenced image layers
(`<none>` in `docker images`) and build cache behind — these are the
biggest source of wasted disk space on this machine, easily tens of GB:

```bash
docker images --filter "dangling=true" -q | xargs -r docker rmi
docker builder prune -af
```

Check overall usage any time with `docker system df -v`.

**Keep project-specific:** this Docker install should only ever contain
images/containers/volumes for *this* project. If `docker ps -a` /
`docker images` turns up containers or images from some other past project
(a different app name, a Docker Desktop extension, an old experiment), that
isn't this project's business to assume — stop and figure out what it is
before removing it, since volumes in particular are **irreversible** to
delete (they hold database data with no backup elsewhere). Don't run
`docker system prune -a --volumes` or any other blanket wipe — it doesn't
distinguish this project's data from anything else on the machine.

## Quick reference

| Goal                                   | Command                                                          |
|-----------------------------------------|--------------------------------------------------------------------|
| Pick up a `backend/` code edit          | `docker compose restart app` (no rebuild, no auto-trigger on save) |
| Start fresh / after a dependency change | `docker compose up --build -d`                                    |
| Full reset (drift, stray containers)    | `docker compose down --remove-orphans -t 30 && docker compose up -d` |
| See only this project's containers      | `docker compose ps -a`                                             |
| Tail app logs                           | `docker compose logs -f app`                                       |
| Full stop, keep data                    | `docker compose down --remove-orphans`                             |
| Full stop, **wipe** mongo/postgres data | `docker compose down -v` (destructive — confirm first)             |
| Free disk from old image layers         | `docker images --filter "dangling=true" -q \| xargs -r docker rmi` |
| Check what's using disk                 | `docker system df -v`                                               |
