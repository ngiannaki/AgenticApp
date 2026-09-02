from pathlib import Path
from uuid import UUID


class DocumentStore:

    def __init__(self, root: str | Path = "data/documents"):
        self.root = Path(root)
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
        Return the PDF path for a document.
        """

        document_dir = self.get_document_dir(document_id)

        return document_dir / "document.pdf"

    def create_document(
        self,
        pdf_bytes: bytes,
        document_id: str,
    ) -> Path:
        """
        Store a new PDF.

        Creates:

        data/documents/{document_id}/document.pdf
        """

        document_dir = self.get_document_dir(
            document_id
        )

        document_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        pdf_path = document_dir / "document.pdf"

        pdf_path.write_bytes(pdf_bytes)

        return pdf_path

    def exists(self, document_id: str) -> bool:
        return self.get_pdf_path(document_id).exists()

    def delete(self, document_id: str) -> None:

        document_dir = self.get_document_dir(
            document_id
        )

        if not document_dir.exists():
            return

        import shutil

        shutil.rmtree(document_dir)