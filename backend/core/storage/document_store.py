from pathlib import Path
from uuid import UUID

from backend.config import settings


class DocumentStore:

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or settings.document_store.dir)
        self.root.mkdir(
            parents=True,
            exist_ok=True,
        )

    def get_document_dir(self, document_id: str) -> Path:
        """
        Return the directory belonging to a document.
        """

        # Validate that the ID is a UUID.
        UUID(document_id)

        return self.root / document_id

    def get_pdf_path(self, document_id: str) -> Path:
        """
        Return the PDF path for a document. Back-compat wrapper for callers
        that only ever dealt with PDFs (server.py's parse_document) — new
        code should use get_file_path, which works for any stored extension.
        """

        return self.get_document_dir(document_id) / "original.pdf"

    def get_file_path(self, document_id: str) -> Path | None:
        """
        Return the stored file's path regardless of its original extension
        (stored as `original{ext}`), or None if nothing has been stored yet.
        """

        document_dir = self.get_document_dir(document_id)
        if not document_dir.exists():
            return None

        matches = list(document_dir.glob("original.*"))
        return matches[0] if matches else None

    def create_document(
        self,
        file_bytes: bytes,
        document_id: str,
        filename: str = "document.pdf",
    ) -> Path:
        """
        Store a new file of any supported type.

        Creates:

        data/documents/{document_id}/original{ext}

        where {ext} is taken from `filename` (defaults to .pdf for back-compat
        with callers that only ever pass raw PDF bytes).
        """

        document_dir = self.get_document_dir(
            document_id
        )

        document_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        extension = Path(filename).suffix or ".pdf"
        file_path = document_dir / f"original{extension}"

        file_path.write_bytes(file_bytes)

        return file_path

    def exists(self, document_id: str) -> bool:
        return self.get_file_path(document_id) is not None

    def delete(self, document_id: str) -> None:

        document_dir = self.get_document_dir(
            document_id
        )

        if not document_dir.exists():
            return

        import shutil

        shutil.rmtree(document_dir)