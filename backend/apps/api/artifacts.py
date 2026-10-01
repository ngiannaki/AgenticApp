"""The API's half of the code-sandbox contract.

1. Inline artifacts -> URLs. The code MCP server (in its own container, no
   shared disk) returns generated files inline: `{filename, content_type,
   size_bytes, data_base64}`. `materialize_artifacts` stores each one in the
   ArtifactStore and swaps the bytes for a `url` *before* the result reaches
   the model, the UI, or conversation history — so a 5MB plot never enters
   the LLM context, and links work the same as before.

2. One-time file tokens. The sandbox needs the bytes of a spreadsheet the
   user attached, but must not be able to read the shared document folder
   (every user's files) or hold any secret. So for each spreadsheet tool
   call the API issues a random, single-use, short-lived token bound to one
   document the caller owns, injects it as the call's `file_token`, and the
   sandbox redeems it once at GET /internal/files/{token}.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from backend.core.storage.artifact_store import ArtifactStore

logger = logging.getLogger("agentic_api")

FILE_TOKEN_TTL_SECONDS = 120.0


def _store_inline(node: dict[str, Any], store: ArtifactStore, base_url: str) -> dict[str, Any]:
    try:
        data = base64.b64decode(node["data_base64"], validate=True)
    except (binascii.Error, ValueError, TypeError):
        logger.warning("Dropping malformed inline artifact %r", node.get("filename"))
        return {k: v for k, v in node.items() if k != "data_base64"}
    # The name comes from the sandbox (ultimately from user code), so keep
    # only its last path component.
    filename = re.split(r"[\\/]", str(node.get("filename") or ""))[-1].strip()
    if filename in ("", ".", ".."):
        filename = "file"
    try:
        artifact_id = store.save(data, filename)
    except (ValueError, OSError):
        logger.warning("Could not store inline artifact %r", filename, exc_info=True)
        return {k: v for k, v in node.items() if k != "data_base64"}
    out = {k: v for k, v in node.items() if k != "data_base64"}
    out["filename"] = filename
    out["size_bytes"] = len(data)
    out["url"] = f"{base_url.rstrip('/')}/api/files/{artifact_id}/{quote(filename)}"
    return out


def _walk(node: Any, store: ArtifactStore, base_url: str) -> Any:
    if isinstance(node, dict):
        if isinstance(node.get("data_base64"), str) and "filename" in node:
            return _store_inline(node, store, base_url)
        return {k: _walk(v, store, base_url) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, store, base_url) for v in node]
    return node


def materialize_artifacts(text: str, store: ArtifactStore, base_url: str) -> str:
    """Replace every inline artifact in a JSON tool result with a stored-file
    URL. Non-JSON text, or JSON without inline artifacts, comes back as-is."""
    if '"data_base64"' not in text:
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    return json.dumps(_walk(data, store, base_url))


@dataclass
class _Grant:
    document_id: str
    expires_at: float


class FileTokenStore:
    """Single-use, short-lived download grants for the code sandbox.

    In memory: the API is one process, and a grant only has to live for the
    seconds between issuing it and the sandbox fetching the file.
    """

    def __init__(self, ttl_seconds: float = FILE_TOKEN_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._grants: dict[str, _Grant] = {}

    def issue(self, document_id: str) -> str:
        self._purge()
        token = secrets.token_urlsafe(32)
        self._grants[token] = _Grant(document_id, time.monotonic() + self._ttl)
        return token

    def redeem(self, token: str) -> str | None:
        """The document_id the token grants, once; None if unknown, used or expired."""
        grant = self._grants.pop(token, None)
        if grant is None or grant.expires_at < time.monotonic():
            return None
        return grant.document_id

    def _purge(self) -> None:
        now = time.monotonic()
        for token in [t for t, g in self._grants.items() if g.expires_at < now]:
            self._grants.pop(token, None)
