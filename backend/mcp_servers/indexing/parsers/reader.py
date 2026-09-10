from pathlib import Path

from backend.mcp_servers.indexing.parsers.csv_reader import parse_csv
from backend.mcp_servers.indexing.parsers.detect import detect_document_type
from backend.mcp_servers.indexing.parsers.docx_reader import parse_docx
from backend.mcp_servers.indexing.parsers.pdf import parse_pdf
from backend.mcp_servers.indexing.parsers.pdf_markdown import parse_pdf_markdown
from backend.mcp_servers.indexing.parsers.pptx_reader import parse_pptx
from backend.mcp_servers.indexing.parsers.txt_reader import parse_txt
from backend.mcp_servers.indexing.parsers.xlsx_reader import parse_xlsx

VALID_FORMATS = ("markdown", "text")


def read_document(file_path: str | Path, format: str = "markdown") -> dict:
    """Detect a document's type, parse it, and render it as markdown or text.

    `format`: "markdown" (default) — tables and, for PDFs, headings are
    rendered as real markdown (Docling-backed for PDF; native table syntax
    for CSV/XLSX). "text" — flat plain text, no markdown syntax.

    Returns the parser's own result dict (shape varies by type — see
    parse_pdf/parse_csv/parse_xlsx/parse_txt) plus `"type"`, `"format"`, and
    `"content"` (the rendering in the requested format) merged in, so
    callers that just want readable text can use `content` without caring
    about the type-specific fields.
    """

    if format not in VALID_FORMATS:
        raise ValueError(f"format must be one of {VALID_FORMATS}, got {format!r}")

    file_path = Path(file_path)
    doc_type = detect_document_type(file_path)

    if doc_type == "pdf":
        result = parse_pdf(file_path)
        result["content"] = (
            parse_pdf_markdown(file_path, result["pages"])
            if format == "markdown"
            else result["full_text"]
        )
    elif doc_type == "csv":
        result = parse_csv(file_path)
        result["content"] = result["markdown"] if format == "markdown" else result["text"]
    elif doc_type == "xlsx":
        result = parse_xlsx(file_path)
        result["content"] = result["markdown"] if format == "markdown" else result["text"]
    elif doc_type == "docx":
        result = parse_docx(file_path)
        result["content"] = result["markdown"] if format == "markdown" else result["text"]
    elif doc_type == "pptx":
        result = parse_pptx(file_path)
        result["content"] = result["full_text"]
    else:  # txt
        result = parse_txt(file_path)
        result["content"] = result["full_text"]

    result["type"] = doc_type
    result["format"] = format

    return result
