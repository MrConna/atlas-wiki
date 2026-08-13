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
