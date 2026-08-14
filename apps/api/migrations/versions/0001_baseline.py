"""Adopt or create the Atlas MVP schema."""
from alembic import op
import sqlalchemy as sa


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


EXPECTED = {
    "pages": {"id", "title", "slug", "content", "source_type", "created_at", "updated_at"},
    "documents": {"id", "page_id", "filename", "media_type", "content_hash", "storage_path", "size_bytes", "status", "created_at"},
    "chunks": {"id", "page_id", "content", "heading_path", "source_location", "position", "embedding"},
}

EXPECTED_COLUMNS = {
    "pages": {
        "id": (sa.String, 36, False), "title": (sa.String, 240, False),
        "slug": (sa.String, 260, False), "content": (sa.Text, None, False),
        "source_type": (sa.String, 32, False), "created_at": (sa.DateTime, None, False),
        "updated_at": (sa.DateTime, None, False),
    },
    "documents": {
        "id": (sa.String, 36, False), "page_id": (sa.String, 36, False),
        "filename": (sa.String, 255, False), "media_type": (sa.String, 100, False),
        "content_hash": (sa.String, 64, False), "storage_path": (sa.String, 500, False),
        "size_bytes": (sa.Integer, None, False), "status": (sa.String, 32, False),
        "created_at": (sa.DateTime, None, False),
    },
    "chunks": {
        "id": (sa.String, 36, False), "page_id": (sa.String, 36, False),
        "content": (sa.Text, None, False), "heading_path": (sa.String, 500, True),
        "source_location": (sa.String, 240, True), "position": (sa.Integer, None, False),
        "embedding": (sa.JSON, None, True),
    },
}


def _validate_existing_schema(inspector) -> None:
    for table, required in EXPECTED.items():
        inspected_columns = inspector.get_columns(table)
        columns = {column["name"] for column in inspected_columns}
        if not required <= columns:
            raise RuntimeError(f"Atlas table {table} is missing required baseline columns")
        primary_key = set(inspector.get_pk_constraint(table).get("constrained_columns") or [])
        if primary_key != {"id"}:
            raise RuntimeError(f"Atlas table {table} does not have the expected primary key")
        by_name = {column["name"]: column for column in inspected_columns}
        for name, (type_class, length, nullable) in EXPECTED_COLUMNS[table].items():
            column = by_name[name]
            if not isinstance(column["type"], type_class):
                raise RuntimeError(f"Atlas table {table}.{name} has an unexpected type")
            if length is not None and getattr(column["type"], "length", None) != length:
                raise RuntimeError(f"Atlas table {table}.{name} has an unexpected length")
            if column["nullable"] is not nullable:
                raise RuntimeError(f"Atlas table {table}.{name} has unexpected nullability")

    unique_columns = {
        table: {
            tuple(constraint.get("column_names") or constraint.get("constrained_columns") or [])
            for constraint in inspector.get_unique_constraints(table)
        }
        | {
            tuple(index.get("column_names") or [])
            for index in inspector.get_indexes(table)
            if index.get("unique")
        }
        for table in EXPECTED
    }
    for table, column in (("pages", "slug"), ("documents", "page_id"), ("documents", "content_hash")):
        if (column,) not in unique_columns[table]:
            raise RuntimeError(f"Atlas table {table}.{column} is missing its unique constraint")

    for table in ("documents", "chunks"):
        valid_fk = any(
            fk.get("referred_table") == "pages"
            and fk.get("constrained_columns") == ["page_id"]
            and fk.get("referred_columns") == ["id"]
            and (fk.get("options") or {}).get("ondelete", "").upper() == "CASCADE"
            for fk in inspector.get_foreign_keys(table)
        )
        if not valid_fk:
            raise RuntimeError(f"Atlas table {table}.page_id is missing its cascading pages foreign key")

    indexes = {
        table: {tuple(index.get("column_names") or []) for index in inspector.get_indexes(table)}
        for table in EXPECTED
    }
    for table, columns in (("pages", ("title",)), ("chunks", ("page_id",))):
        if columns not in indexes[table]:
            raise RuntimeError(f"Atlas table {table} is missing its expected index on {columns[0]}")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names()) - {"alembic_version"}
    existing_core = tables & set(EXPECTED)
    if existing_core:
        if existing_core != set(EXPECTED):
            raise RuntimeError("Partial Atlas schema cannot be adopted safely")
        _validate_existing_schema(inspector)
        return

    op.create_table(
        "pages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("slug", sa.String(260), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pages_title", "pages", ["title"])
    op.create_index("ix_pages_slug", "pages", ["slug"], unique=True)
    op.create_table(
        "documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("page_id", sa.String(36), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_documents_page_id", "documents", ["page_id"], unique=True)
    op.create_index("ix_documents_content_hash", "documents", ["content_hash"], unique=True)
    op.create_table(
        "chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("page_id", sa.String(36), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("heading_path", sa.String(500)),
        sa.Column("source_location", sa.String(240)),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("embedding", sa.JSON()),
    )
    op.create_index("ix_chunks_page_id", "chunks", ["page_id"])


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_table("documents")
    op.drop_table("pages")
