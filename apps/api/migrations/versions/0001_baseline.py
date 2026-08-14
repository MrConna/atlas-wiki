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


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names()) - {"alembic_version"}
    existing_core = tables & set(EXPECTED)
    if existing_core:
        if existing_core != set(EXPECTED):
            raise RuntimeError("Partial Atlas schema cannot be adopted safely")
        for table, required in EXPECTED.items():
            columns = {column["name"] for column in inspector.get_columns(table)}
            if not required <= columns:
                raise RuntimeError(f"Atlas table {table} is missing required baseline columns")
        return

    op.create_table(
        "pages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("slug", sa.String(260), nullable=False, unique=True),
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
        sa.Column("page_id", sa.String(36), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
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
