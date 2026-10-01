# Single-container build of the Agentic API + its 4 MCP servers (code, db,
# indexing, retrieval). The API launches the MCP servers itself as stdio
# subprocesses (see backend/apps/client/servers.docker.json) instead of each
# running as its own long-lived HTTP service — this is the deploy-friendly
# alternative to start_mcp_servers.ps1, which still works unchanged for
# local dev. See RUNNING.md for both ways to run this app.
#
# External service this container does NOT include and still needs:
#   - Postgres with pgvector (DATABASE_URL / DB_MCP_DATABASE_URL) — users,
#     chats, documents, the vector index and per-user DB-tool tables.
# docker-compose.yml runs it (pgvector/pgvector:pg16) next to this container.
FROM python:3.13-slim

WORKDIR /app

# docling (indexing's PDF parser) and sentence-transformers (retrieval's
# reranker) pull in torch and native extensions; libgomp1 is the one shared
# library those wheels need at runtime that slim doesn't ship by default.
# docling's OCR fallback (rapidocr) pulls in opencv-python — the GUI build,
# not opencv-python-headless — which needs a handful of X11/graphics shared
# libraries even though nothing here ever opens a window. Without these,
# docling's PDF conversion fails at import time with e.g.
# "libxcb.so.1: cannot open shared object file" (found by actually
# uploading a real PDF against this image, not just importing modules).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libxcb1 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements-base.txt backend/requirements-base.txt
COPY backend/apps/api/requirements.txt backend/apps/api/requirements.txt
COPY backend/mcp_servers/code/requirements.txt backend/mcp_servers/code/requirements.txt
COPY backend/mcp_servers/db/requirements.txt backend/mcp_servers/db/requirements.txt
COPY backend/mcp_servers/indexing/requirements.txt backend/mcp_servers/indexing/requirements.txt
COPY backend/mcp_servers/retrieval/requirements.txt backend/mcp_servers/retrieval/requirements.txt

RUN pip install --no-cache-dir -r backend/requirements-base.txt \
    && pip install --no-cache-dir -r backend/apps/api/requirements.txt \
    && pip install --no-cache-dir -r backend/mcp_servers/code/requirements.txt \
    && pip install --no-cache-dir -r backend/mcp_servers/db/requirements.txt \
    && pip install --no-cache-dir -r backend/mcp_servers/indexing/requirements.txt \
    && pip install --no-cache-dir -r backend/mcp_servers/retrieval/requirements.txt

COPY backend/ backend/

# Selects the stdio variant of the MCP server mapping (default servers.json
# uses http://localhost:8000-8003, which don't exist as separate services
# in this image). settings.py still reads backend/.env for every actual
# secret/config value — mount or bake that file in separately; it is not
# copied by this Dockerfile (see .dockerignore) since it holds secrets.
ENV API_SERVERS_PATH=/app/backend/apps/client/servers.docker.json
ENV PYTHONPATH=/app

EXPOSE 8100

CMD ["python", "-m", "backend.apps.api.main"]
