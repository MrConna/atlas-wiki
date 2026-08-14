#!/usr/bin/env python3
"""Read-only post-restore integrity verification for database and uploaded files."""

import hashlib
import sys
from pathlib import Path

from sqlalchemy import select, text

from app.config import settings
from app.database import SessionLocal
from app.models import Chunk, Document, Page


def main() -> int:
    failures: list[str] = []
    upload_root = Path(settings.upload_dir).resolve()
    with SessionLocal() as db:
        version = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
        if not version:
            failures.append("database has no Alembic version")
        page_ids = set(db.scalars(select(Page.id)).all())
        documents = list(db.scalars(select(Document)).all())
        chunks = list(db.scalars(select(Chunk)).all())
        referenced = set()
        for document in documents:
            path = Path(document.storage_path).resolve()
            referenced.add(path)
            if upload_root not in path.parents:
                failures.append(f"document {document.id} points outside upload directory")
                continue
            if not path.is_file():
                failures.append(f"document {document.id} file is missing")
                continue
            if path.stat().st_size != document.size_bytes:
                failures.append(f"document {document.id} size does not match")
            if hashlib.sha256(path.read_bytes()).hexdigest() != document.content_hash:
                failures.append(f"document {document.id} checksum does not match")
            if document.page_id not in page_ids:
                failures.append(f"document {document.id} references a missing page")
        for chunk in chunks:
            if chunk.page_id not in page_ids:
                failures.append(f"chunk {chunk.id} references a missing page")
        for path in upload_root.iterdir():
            if path.name == ".gitkeep":
                continue
            if path.is_symlink() or not path.is_file():
                failures.append(f"unsafe upload entry: {path.name}")
            elif path.resolve() not in referenced:
                failures.append(f"unreferenced upload: {path.name}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"PASS: version={version} pages={len(page_ids)} documents={len(documents)} chunks={len(chunks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
