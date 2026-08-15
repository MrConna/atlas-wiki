"""Add an optional category to pages."""
from alembic import op
import sqlalchemy as sa


revision = "0003_page_category"
down_revision = "0002_pgvector_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pages") as batch:
        batch.add_column(sa.Column("category", sa.String(60), nullable=True))
    op.create_index("ix_pages_category", "pages", ["category"])


def downgrade() -> None:
    op.drop_index("ix_pages_category", table_name="pages")
    with op.batch_alter_table("pages") as batch:
        batch.drop_column("category")
