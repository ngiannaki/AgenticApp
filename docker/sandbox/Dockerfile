# Code sandbox: the Code MCP server in its own locked-down container.
#
# What makes it a sandbox is mostly how it's *run* (docker-compose.yml's
# `code-sandbox` service, or start_mcp_servers.ps1's `docker run`):
#   - read-only root filesystem; writable tmpfs only at /work and /tmp
#   - only the capabilities needed to start each run as its own runner uid
#     (SETUID, SETGID, CHOWN, DAC_OVERRIDE, KILL), no-new-privileges
#   - memory / pids / cpu limits, and `init` to reap stray processes
#   - on an internal network: no internet, no Postgres, only the API
#   - no secrets in its environment (backend/.env is never copied in)
# Inside, each submission runs as an unprivileged uid with rlimits — see
# backend/mcp_servers/code/execution/sandbox.py.
FROM python:3.13-slim

WORKDIR /app

COPY docker/sandbox/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Only the code server and what it imports.
COPY backend/__init__.py backend/__init__.py
COPY backend/config backend/config
COPY backend/core/storage backend/core/storage
COPY backend/core/mcp_http.py backend/core/mcp_http.py
COPY backend/mcp_servers/__init__.py backend/mcp_servers/__init__.py
COPY backend/mcp_servers/code backend/mcp_servers/code

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=http \
    CODE_MCP_HOST=0.0.0.0 \
    CODE_MCP_PORT=8003 \
    CODE_MCP_ISOLATION=uid \
    CODE_MCP_REQUIRE_ISOLATION=true \
    CODE_MCP_WORKSPACE_DIR=/work \
    CODE_MCP_PYTHON_EXECUTABLE=/usr/local/bin/python \
    CODE_MCP_ENV_PASSTHROUGH='["PATH"]'

EXPOSE 8003
CMD ["python", "-m", "backend.mcp_servers.code.server"]
