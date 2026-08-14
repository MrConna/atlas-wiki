import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_fresh_sqlite_migration_preserves_legacy_embedding_column(tmp_path):
    api_dir = Path(__file__).parents[1]
    database_path = tmp_path / "atlas.db"
    environment = os.environ | {"DATABASE_URL": f"sqlite:///{database_path}"}

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=api_dir,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]

    assert {"legacy_embedding", "embedding", "embedding_model", "embedding_version"} <= columns
    assert version == "0002_pgvector_embeddings"


def test_existing_sqlite_migration_preserves_legacy_embedding_data(tmp_path):
    api_dir = Path(__file__).parents[1]
    database_path = tmp_path / "legacy.db"
    environment = os.environ | {"DATABASE_URL": f"sqlite:///{database_path}"}

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0001_baseline"],
        cwd=api_dir,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO pages (id, slug, title, content, source_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("page-1", "page", "Page", "content", "native", "2026-01-01", "2026-01-01"),
        )
        connection.execute(
            "INSERT INTO chunks (id, page_id, content, position, embedding) VALUES (?, ?, ?, ?, ?)",
            ("chunk-1", "page-1", "content", 0, "[0.4, 0.5]"),
        )
        connection.commit()

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=api_dir,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        legacy, current = connection.execute(
            "SELECT legacy_embedding, embedding FROM chunks WHERE id = 'chunk-1'"
        ).fetchone()

    assert legacy == "[0.4, 0.5]"
    assert current is None

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0001_baseline"],
        cwd=api_dir,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        restored = connection.execute("SELECT embedding FROM chunks WHERE id = 'chunk-1'").fetchone()[0]
    assert restored == "[0.4, 0.5]"


def test_baseline_rejects_schema_without_integrity_constraints(tmp_path):
    api_dir = Path(__file__).parents[1]
    database_path = tmp_path / "unsafe.db"
    environment = os.environ | {"DATABASE_URL": f"sqlite:///{database_path}"}
    definitions = {
        "pages": ("id", "title", "slug", "content", "source_type", "created_at", "updated_at"),
        "documents": ("id", "page_id", "filename", "media_type", "content_hash", "storage_path", "size_bytes", "status", "created_at"),
        "chunks": ("id", "page_id", "content", "heading_path", "source_location", "position", "embedding"),
    }
    with sqlite3.connect(database_path) as connection:
        for table, columns in definitions.items():
            connection.execute(f"CREATE TABLE {table} ({', '.join(f'{column} TEXT' for column in columns)})")

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=api_dir,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "expected primary key" in result.stderr
