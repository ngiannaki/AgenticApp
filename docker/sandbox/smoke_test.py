"""Smoke test for a running code-sandbox container, from inside it.

    docker exec -i <sandbox-container> python - < docker/sandbox/smoke_test.py

Calls the real MCP server on localhost:8003 and checks that submitted code
runs as an unprivileged uid, can't read the server's environment, and has
no internet access. Exits non-zero on any failure (used by CI).
"""
import asyncio
import json
import sys

from fastmcp import Client

CODE = r"""
import os, socket
print("uid", os.getuid())
try:
    open("/proc/1/environ").read()
    print("environ READABLE")
except PermissionError:
    print("environ denied")
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3)
    print("internet REACHABLE")
except OSError:
    print("internet blocked")
try:
    open("/app/backend/hacked.py", "w")
    print("filesystem WRITABLE")
except OSError:
    print("filesystem read-only")
"""


async def main() -> int:
    async with Client("http://127.0.0.1:8003/mcp") as client:
        result = await client.call_tool("run_python_code", {"code": CODE})
    data = json.loads(result.content[0].text)
    out = data.get("stdout", "")
    print(out or data)
    checks = {
        "runs unprivileged": "uid 0" not in out and "uid " in out,
        "server env unreadable": "environ denied" in out,
        "no internet": "internet blocked" in out,
        "read-only filesystem": "filesystem read-only" in out,
    }
    for name, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(checks.values()) else 1


sys.exit(asyncio.run(main()))
