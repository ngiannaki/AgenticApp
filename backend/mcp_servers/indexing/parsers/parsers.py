"""
parsers.py  (clean/graphs)
--------------------------
Self-contained PDF parser resolver for the clean/graphs pipeline.

Mirrors the parser selection logic in src/datasage but has no dependency
on the datasage package, making clean/graphs independently deployable.

Backend selection
-----------------
Controlled by the USE_GOOGLE_PARSER environment variable (or .env file):

    USE_GOOGLE_PARSER=true   → Google Document AI (OCR-grade, cloud)
    USE_GOOGLE_PARSER=false  → IBM Docling  (default, runs locally)

    parser_type="auto"   → respects USE_GOOGLE_PARSER flag
    parser_type="google" → always use Google DocAI
    parser_type="docling"→ always use Docling

Override at runtime (e.g. in a notebook):

    from clean.graphs.parsers import set_use_google_parser
    set_use_google_parser(True)    # switch to Google DocAI
    set_use_google_parser(False)   # switch to Docling

Environment variables
---------------------
    USE_GOOGLE_PARSER          default: false
    GOOGLE_DOCAI_PROJECT_ID    default: project-db0446e6-5527-4b1b-ba4
    GOOGLE_DOCAI_LOCATION      default: eu
    GOOGLE_DOCAI_PROCESSOR_ID  default: 45e6c24dbbd65e16
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, List, Optional

from backend.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GOOGLE_DOCAI_PROJECT_ID = settings.google_docai.project_id
GOOGLE_DOCAI_LOCATION = settings.google_docai.location
GOOGLE_DOCAI_PROCESSOR_ID = settings.google_docai.processor_id

# Bootstrap Google ADC from a configured credentials file if the env var isn't already set
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and settings.google_docai.credentials_path:
    _credentials_path = Path(settings.google_docai.credentials_path)
    if _credentials_path.exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_credentials_path)

# ---------------------------------------------------------------------------
# Runtime flag (mirrors settings.google_docai.use_parser)
# ---------------------------------------------------------------------------

_USE_GOOGLE_PARSER: bool = settings.google_docai.use_parser


def get_use_google_parser() -> bool:
    """Return the current parser backend selection."""
    return _USE_GOOGLE_PARSER


def set_use_google_parser(enabled: bool) -> None:
    """
    Switch the PDF parser backend at runtime.

    Parameters
    ----------
    enabled : bool
        True  → Google Document AI (cloud, OCR-grade)
        False → IBM Docling (local, layout-aware)
    """
    global _USE_GOOGLE_PARSER
    _USE_GOOGLE_PARSER = bool(enabled)
    logger.info("Parser backend set to: %s", "Google DocAI" if enabled else "Docling")


# ---------------------------------------------------------------------------
# Parser factory
# ---------------------------------------------------------------------------


def get_parser(
    parser_type: str = "auto",
    use_ocr: bool = False,
) -> Any:
    """
    Resolve and return the configured document parser.

    Parameters
    ----------
    parser_type : str
        "auto"    → respects the USE_GOOGLE_PARSER flag / set_use_google_parser()
        "google"  → force Google Document AI
        "docling" → force IBM Docling
    use_ocr : bool
        Enable OCR pipeline (Docling only; Google DocAI always uses OCR).

    Returns
    -------
    A parser instance with a ``parse_file(path) → List[PageContent]`` method.
    """
    use_google = (
        _USE_GOOGLE_PARSER if parser_type == "auto" else parser_type == "google"
    )

    if use_google:
        try:
            from clean.graphs.google_document_parser import GoogleDocumentParser
            parser = GoogleDocumentParser(
                project_id=GOOGLE_DOCAI_PROJECT_ID,
                location=GOOGLE_DOCAI_LOCATION,
                processor_id=GOOGLE_DOCAI_PROCESSOR_ID,
            )
            logger.debug(
                "Using Google DocAI parser (project=%s location=%s processor=%s)",
                GOOGLE_DOCAI_PROJECT_ID, GOOGLE_DOCAI_LOCATION, GOOGLE_DOCAI_PROCESSOR_ID,
            )
            return parser
        except Exception as exc:
            logger.error(
                "Failed to initialise Google DocAI parser; falling back to Docling. Error: %s",
                exc,
                exc_info=True,
            )

    # Docling (default / fallback)
    from clean.graphs.document_parser import DocumentParser
    parser = DocumentParser(use_ocr=use_ocr)
    logger.debug(
        "Using Docling parser (ocr=%s, converter_loaded=%s)",
        use_ocr, parser._converter is not None,
    )
    return parser


def parser_name(parser_type: str = "auto", use_ocr: bool = False) -> str:
    """
    Return a human-readable label for the active parser
    without instantiating it — useful for logging.
    """
    use_google = (
        _USE_GOOGLE_PARSER if parser_type == "auto" else parser_type == "google"
    )
    if use_google:
        return "google"
    return "docling" if not use_ocr else "docling+ocr"
