from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON


EMBEDDING_DIMENSIONS = 768
EmbeddingVector = Vector(EMBEDDING_DIMENSIONS).with_variant(JSON(none_as_null=True), "sqlite")
