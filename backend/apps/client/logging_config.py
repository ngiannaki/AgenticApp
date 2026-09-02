"""Logging setup for the MCP agent client — rich, human-readable console logs."""
from __future__ import annotations

import logging

from rich.logging import RichHandler

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure the 'mcp_agent' logger tree with a RichHandler (idempotent)."""
    global _CONFIGURED
    logger = logging.getLogger("mcp_agent")
    if not _CONFIGURED:
        handler = RichHandler(show_time=True, show_path=False, markup=True, rich_tracebacks=True)
        handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
        logger.addHandler(handler)
        logger.propagate = False
        _CONFIGURED = True
    logger.setLevel(level.upper())
    return logger
