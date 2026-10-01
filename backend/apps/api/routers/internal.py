"""Endpoints for the app's own services, not for browsers.

GET /internal/files/{token}: the code sandbox redeems a single-use
`file_token` (issued by streaming_agent for one spreadsheet tool call, see
backend/apps/api/artifacts.py) for the bytes of the one document it grants.
No user auth — the token *is* the authorization: random, single-use,
expiring within minutes, and only ever issued for a document that just
passed the caller's ownership check. As defence in depth it's only served
to private/loopback addresses, and the edge proxies (Caddy/nginx) never
route /internal/ at all.
"""
from __future__ import annotations

import ipaddress
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from backend.apps.api.routers.files import _document_store

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)
logger = logging.getLogger("agentic_api")


def _is_internal_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


@router.get("/files/{token}")
def redeem_file(token: str, request: Request) -> FileResponse:
    if not _is_internal_client(request):
        raise HTTPException(status_code=404)
    document_id = request.app.state.runtime.file_tokens.redeem(token)
    if document_id is None:
        raise HTTPException(status_code=404)
    path = _document_store.get_file_path(document_id)
    if path is None:
        raise HTTPException(status_code=404)
    logger.info("[INTERNAL] file token redeemed for document %s", document_id)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"X-Document-Id": document_id, "X-Document-Suffix": path.suffix.lower()},
    )
