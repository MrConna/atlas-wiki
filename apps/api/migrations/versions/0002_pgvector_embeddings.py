"""Preserve legacy hashes and add native multilingual embeddings."""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision = "0002_pgvector_embeddings"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    vector_type = Vector(768) if is_postgres else sa.JSON(none_as_null=True)
    with op.batch_alter_table("chunks") as batch:
        batch.alter_column("embedding", new_column_name="legacy_embedding", existing_type=sa.JSON())
        batch.add_column(sa.Column("embedding", vector_type, nullable=True))
        batch.add_column(sa.Column("embedding_model", sa.String(255), nullable=True))
        batch.add_column(sa.Column("embedding_version", sa.String(128), nullable=True))
        batch.add_column(sa.Column("embedding_updated_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "ck_chunks_embedding_metadata_complete",
            "(embedding IS NULL AND embedding_model IS NULL AND embedding_version IS NULL AND embedding_updated_at IS NULL) "
            "OR (embedding IS NOT NULL AND embedding_model IS NOT NULL AND embedding_version IS NOT NULL AND embedding_updated_at IS NOT NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("chunks") as batch:
        batch.drop_constraint("ck_chunks_embedding_metadata_complete", type_="check")
        batch.drop_column("embedding_updated_at")
        batch.drop_column("embedding_version")
        batch.drop_column("embedding_model")
        batch.drop_column("embedding")
        batch.alter_column("legacy_embedding", new_column_name="embedding", existing_type=sa.JSON())
