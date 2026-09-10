"""Document type detection.

Used by read_document to pick the right parser without the caller having to
know or guess a file's format up front. Extension is trusted first (cheap,
almost always right); magic bytes are only consulted to catch a mislabeled
extension, and only for the two binary formats where a signature exists.
"""

from pathlib import Path

SUPPORTED_TYPES = ("pdf", "csv", "xlsx", "txt", "docx", "pptx")

_EXTENSION_MAP = {
    ".pdf": "pdf",
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".txt": "txt",
    ".md": "txt",
    ".log": "txt",
    ".docx": "docx",
    ".pptx": "pptx",
}

# Legacy binary Office formats (.doc, .ppt) are intentionally unsupported —
# they need a full OLE2/binary parser (or LibreOffice) that isn't worth
# pulling in; callers get a clear error instead of a silent mis-parse.
_UNSUPPORTED_EXTENSIONS = {".doc", ".ppt"}

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"  # xlsx/docx/pptx are all zip archives under the hood


def detect_document_type(file_path: str | Path) -> str:
    """Return one of SUPPORTED_TYPES for a file on disk.

    Raises FileNotFoundError if the path doesn't exist, or ValueError if the
    type can't be determined (unknown/unsupported extension and no
    recognizable magic bytes).
    """

    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with file_path.open("rb") as f:
        head = f.read(8)

    if head.startswith(_PDF_MAGIC):
        return "pdf"

    if head.startswith(_ZIP_MAGIC):
        # docx/pptx/xlsx are all zip containers — the magic bytes alone can't
        # tell them apart, so trust the extension for these; if it's not one
        # of them, fall through to the extension map / unsupported check below.
        by_extension = _EXTENSION_MAP.get(file_path.suffix.lower())
        if by_extension in ("xlsx", "docx", "pptx"):
            return by_extension

    return detect_document_type_from_filename(file_path.name, fallback_bytes=head + file_path.open("rb").read(4096))


def detect_document_type_from_filename(filename: str, fallback_bytes: bytes = b"") -> str:
    """Like detect_document_type, but for bytes with no path on disk (e.g.
    received over MCP as raw bytes) — types purely from the filename's
    extension, falling back to a text-decode check for an unrecognized one.
    """

    suffix = Path(filename).suffix.lower()

    if suffix in _UNSUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Could not determine document type for {filename}: "
            f"'{suffix}' (legacy Office format) is not supported — save as "
            f"{'.docx' if suffix == '.doc' else '.pptx'} instead"
        )

    extension_type = _EXTENSION_MAP.get(suffix)
    if extension_type is not None:
        return extension_type

    # Unknown extension — fall back to txt only if the content actually
    # decodes as text, so unsupported binaries error out instead of being
    # silently mis-read.
    try:
        fallback_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(
            f"Could not determine document type for {filename}: "
            f"unrecognized extension '{suffix}' and not a known binary format"
        )

    return "txt"
