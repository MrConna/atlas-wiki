# Architecture

## Request flow

1. The browser calls the FastAPI service directly.
2. Imports are size-limited, type-checked, parsed, hashed, and stored under a hash-derived filename.
3. Extracted text becomes a `Page`; structural chunks retain heading and position metadata.
4. Each chunk receives a deterministic local vector. Retrieval combines lexical overlap and cosine similarity.
5. Ask requests pass top evidence to the configured provider through an OpenAI-compatible or Ollama adapter.
6. Answers without valid evidence identifiers are rejected. Valid citations open and highlight the exact source excerpt, with heading or PDF page metadata.

## Storage

- PostgreSQL stores documents, pages, chunks, vectors, and metadata.
- Original files are stored in the mounted `uploads` directory.
- SHA-256 content hashes provide idempotent imports.
- Deleting a page cascades to chunks/document metadata and removes the stored original.
- Imported source pages are immutable, preserving the relationship between extracted text, hash, and original file.

## Deliberate MVP choices

- Embeddings are deterministic local feature-hash vectors, so retrieval works with no credentials or model download.
- Retrieval scans chunk vectors in the application. This is acceptable for the 1,000-document MVP target; a later migration can map embeddings to native pgvector indexes without changing API contracts.
- Schema creation uses SQLAlchemy metadata for clean installations. Production upgrades should introduce Alembic migrations before schema evolution.

## Native embedding migration

Alembic now owns production schema changes. The expand migration preserves the 128-dimensional feature hash in `legacy_embedding` and adds a nullable native `vector(768)` column plus per-chunk model identity and prompt-schema metadata. Existing and new installations both run `alembic upgrade head` before the API starts.

The local embedding provider uses Ollama with EmbeddingGemma retrieval prompts. Query and document vectors are generated in distinct prompt formats, validated for count, dimension, finite values, and nonzero norm, then normalized before persistence. A resumable backfill uses a PostgreSQL advisory lock and commits deterministic batches. Native pgvector reads are enabled only after every current chunk has a vector for the same resolved Ollama model digest and prompt schema.

The first pgvector release uses exact cosine nearest-neighbor search. HNSW is intentionally deferred until after backfill and retrieval evaluation because maintaining an approximate index during bulk writes adds cost and another recall variable.
