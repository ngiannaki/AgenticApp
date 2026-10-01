"""Rebuild the vector index from the original files already on disk.

    python -m backend.scripts.reindex_all              # documents missing from the index
    python -m backend.scripts.reindex_all --all        # every indexed document
    python -m backend.scripts.reindex_all --document-id <id>

Use it after moving to Postgres (the old Chroma vectors aren't copied — the
raw uploads in data/documents are, so re-embedding them is enough), or
after changing the embedding model. Runs the same parse -> chunk -> embed
pipeline as an upload, in this process, so it needs the indexing server's
dependencies (run it inside the app container, or the project's venv).

Spreadsheets attached in chat (status "stored") are skipped: they're
analysed with pandas, never indexed.
"""
from __future__ import annotations

import argparse
import logging
import sys

from backend.config import settings
from backend.core.db import apply_migrations, get_pool
from backend.core.storage.document_store import DocumentStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="re-index every indexed document, not just missing ones")
    parser.add_argument("--document-id", help="re-index just this document")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    apply_migrations(settings.database.url)

    # Heavy imports (docling, embeddings) only once we know there's work to do.
    from backend.mcp_servers.indexing.langchain_pipeline import LangchainIndexPipelineV2
    from backend.mcp_servers.indexing.parsers.detect import detect_document_type_from_filename

    pool = get_pool()
    with pool.connection() as conn:
        query = (
            "SELECT d.id, d.user_id, d.original_name FROM app.documents d"
            " LEFT JOIN rag.documents r ON r.document_id = d.id"
            " WHERE d.status <> 'stored'"
        )
        params: list = []
        if args.document_id:
            query += " AND d.id = %s"
            params.append(args.document_id)
        elif not args.all:
            query += " AND (r.document_id IS NULL OR r.embedding_model <> %s)"
            params.append(settings.embedding_model)
        rows = conn.execute(query + " ORDER BY d.created_at", params).fetchall()

    if not rows:
        print("Nothing to re-index.")
        return 0

    store = DocumentStore()
    indexer = LangchainIndexPipelineV2()
    failures = 0
    for i, row in enumerate(rows, start=1):
        doc_id, name = row["id"], row["original_name"] or "document"
        path = store.get_file_path(doc_id)
        prefix = f"[{i}/{len(rows)}] {name} ({doc_id})"
        if path is None:
            print(f"{prefix}: original file missing from {settings.document_store.dir} — skipped")
            failures += 1
            continue
        try:
            content_type = detect_document_type_from_filename(name, fallback_bytes=path.read_bytes()[:4096])
        except ValueError as exc:
            print(f"{prefix}: {exc} — skipped")
            failures += 1
            continue

        result = indexer.index_document(
            document_id=doc_id, file_path=path, content_type=content_type, document_name=name, user_id=row["user_id"]
        )
        with pool.connection() as conn:
            if result.get("success"):
                conn.execute(
                    "UPDATE app.documents SET status = 'indexed', chunks_indexed = %s, error = NULL,"
                    " updated_at = now() WHERE id = %s",
                    (result["chunks_indexed"], doc_id),
                )
                print(f"{prefix}: {result['chunks_indexed']} chunks")
            else:
                conn.execute(
                    "UPDATE app.documents SET status = 'failed', error = %s, updated_at = now() WHERE id = %s",
                    (result.get("error"), doc_id),
                )
                print(f"{prefix}: FAILED — {result.get('error')}")
                failures += 1

    print(f"Done: {len(rows) - failures} re-indexed, {failures} skipped/failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
