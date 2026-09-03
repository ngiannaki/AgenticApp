"""Back-compat re-export. The FastAPI SSE backend's settings now live in
`backend/config/settings.py` (part of the single-source-of-truth config
module) — import from `backend.config` in new code.
"""
from __future__ import annotations

from backend.config.settings import ApiSettings, get_api_settings

__all__ = ["ApiSettings", "get_api_settings"]
