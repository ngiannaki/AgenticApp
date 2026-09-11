"""LangChain document loading for the v2 indexing pipeline.

Dispatches to LangChain's own community document loaders (PyMuPDFLoader,
Docx2txtLoader, UnstructuredPowerPointLoader, CSVLoader,
UnstructuredExcelLoader, TextLoader) so parsing goes through LangChain's
`Document` abstraction end-to-end. Each loader has a fallback onto this
repo's existing hand-rolled parsers (backend/mcp_servers/indexing/parsers) —
used only if the LangChain loader's extra dependency isn't installed (e.g.
`unstructured` for pptx/xlsx) or the loader itself raises — so v2 indexing
never hard-fails just because an optional parsing dependency is missing.

Every returned Document carries at least `source` (the original filename)
and, where the format has natural units, `page`/`sheet`/`row` in metadata —
the raw grounding info that langchain_pipeline.py stamps document/user
identity onto afterward.
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.documents import Document

logger = logging.getLogger(__name__)


def _fallback_pdf(file_path: Path) -> list[Document]:
    from backend.mcp_servers.indexing.parsers.pdf import parse_pdf

    parsed = parse_pdf(str(file_path))
    pages = parsed.get("pages") or []
    if not pages:
        return [Document(page_content=parsed.get("full_text", ""), metadata={"source": file_path.name})]
    return [
        Document(
            page_content=page.get("text", ""),
            metadata={"source": file_path.name, "page": page.get("page_number", i)},
        )
        for i, page in enumerate(pages)
    ]


def _load_pdf(file_path: Path) -> list[Document]:
    try:
        from langchain_community.document_loaders import PyMuPDFLoader

        docs = PyMuPDFLoader(str(file_path)).load()
        if docs:
            return docs
    except Exception as e:
        logger.warning(f"[LOADER_V2] PyMuPDFLoader failed for {file_path.name}, falling back: {e}")
    return _fallback_pdf(file_path)


def _fallback_docx(file_path: Path) -> list[Document]:
    from backend.mcp_servers.indexing.parsers.docx_reader import parse_docx

    parsed = parse_docx(file_path)
    return [Document(page_content=parsed.get("full_text", ""), metadata={"source": file_path.name})]


def _load_docx(file_path: Path) -> list[Document]:
    try:
        from langchain_community.document_loaders import Docx2txtLoader

        docs = Docx2txtLoader(str(file_path)).load()
        if docs and any(d.page_content.strip() for d in docs):
            return docs
    except Exception as e:
        logger.warning(f"[LOADER_V2] Docx2txtLoader failed for {file_path.name}, falling back: {e}")
    return _fallback_docx(file_path)


def _fallback_pptx(file_path: Path) -> list[Document]:
    from backend.mcp_servers.indexing.parsers.pptx_reader import parse_pptx

    parsed = parse_pptx(file_path)
    pages = parsed.get("pages") or []
    if not pages:
        return [Document(page_content=parsed.get("full_text", ""), metadata={"source": file_path.name})]
    return [
        Document(
            page_content=page.get("text", ""),
            metadata={"source": file_path.name, "slide": page.get("page_number", i)},
        )
        for i, page in enumerate(pages)
    ]


def _load_pptx(file_path: Path) -> list[Document]:
    try:
        from langchain_community.document_loaders import UnstructuredPowerPointLoader

        docs = UnstructuredPowerPointLoader(str(file_path), mode="single").load()
        if docs and any(d.page_content.strip() for d in docs):
            return docs
    except Exception as e:
        logger.warning(f"[LOADER_V2] UnstructuredPowerPointLoader failed for {file_path.name}, falling back: {e}")
    return _fallback_pptx(file_path)


def _fallback_csv(file_path: Path) -> list[Document]:
    from backend.mcp_servers.indexing.parsers.csv_reader import parse_csv

    parsed = parse_csv(file_path)
    headers = parsed.get("headers") or []
    rows = parsed.get("rows") or []
    header_line = ", ".join(headers)
    docs = []
    for i, row in enumerate(rows):
        line = ", ".join(f"{h}: {v}" for h, v in zip(headers, row))
        docs.append(Document(page_content=f"{header_line}\n{line}", metadata={"source": file_path.name, "row": i}))
    return docs or [Document(page_content="", metadata={"source": file_path.name})]


def _load_csv(file_path: Path) -> list[Document]:
    try:
        from langchain_community.document_loaders.csv_loader import CSVLoader

        docs = CSVLoader(str(file_path)).load()
        if docs:
            return docs
    except Exception as e:
        logger.warning(f"[LOADER_V2] CSVLoader failed for {file_path.name}, falling back: {e}")
    return _fallback_csv(file_path)


def _fallback_xlsx(file_path: Path) -> list[Document]:
    from backend.mcp_servers.indexing.parsers.xlsx_reader import parse_xlsx

    parsed = parse_xlsx(file_path)
    docs = []
    for sheet in parsed.get("sheets") or []:
        headers = sheet.get("headers") or []
        header_line = ", ".join(headers)
        for i, row in enumerate(sheet.get("rows") or []):
            line = ", ".join(f"{h}: {v}" for h, v in zip(headers, row))
            docs.append(
                Document(
                    page_content=f"{header_line}\n{line}",
                    metadata={"source": file_path.name, "sheet": sheet.get("name"), "row": i},
                )
            )
    return docs or [Document(page_content="", metadata={"source": file_path.name})]


def _load_xlsx(file_path: Path) -> list[Document]:
    try:
        from langchain_community.document_loaders import UnstructuredExcelLoader

        docs = UnstructuredExcelLoader(str(file_path), mode="elements").load()
        if docs:
            return docs
    except Exception as e:
        logger.warning(f"[LOADER_V2] UnstructuredExcelLoader failed for {file_path.name}, falling back: {e}")
    return _fallback_xlsx(file_path)


def _fallback_txt(file_path: Path) -> list[Document]:
    from backend.mcp_servers.indexing.parsers.txt_reader import parse_txt

    parsed = parse_txt(file_path)
    return [Document(page_content=parsed.get("full_text", ""), metadata={"source": file_path.name})]


def _load_txt(file_path: Path) -> list[Document]:
    try:
        from langchain_community.document_loaders import TextLoader

        docs = TextLoader(str(file_path), autodetect_encoding=True).load()
        if docs:
            return docs
    except Exception as e:
        logger.warning(f"[LOADER_V2] TextLoader failed for {file_path.name}, falling back: {e}")
    return _fallback_txt(file_path)


_LOADERS = {
    "pdf": _load_pdf,
    "docx": _load_docx,
    "pptx": _load_pptx,
    "csv": _load_csv,
    "xlsx": _load_xlsx,
    "txt": _load_txt,
}


def load_documents_v2(file_path: Path, content_type: str) -> list[Document]:
    """Load `file_path` (of `content_type`, one of parsers.detect.SUPPORTED_TYPES)
    into a list of LangChain `Document`s ready for splitting."""
    loader = _LOADERS.get(content_type)
    if loader is None:
        raise ValueError(f"Unsupported content type for v2 loader: {content_type}")
    docs = loader(Path(file_path))
    return [d for d in docs if d.page_content and d.page_content.strip()]
