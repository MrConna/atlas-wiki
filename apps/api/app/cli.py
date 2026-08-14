import argparse
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, text

from .database import SessionLocal
from .embeddings import PROMPT_SCHEMA_VERSION, EmbeddingError, get_embedding_provider
from .models import Chunk, Page


ADVISORY_LOCK_ID = 0x41544C4153454D42


def pending_clause(identity: str):
    return or_(
        Chunk.embedding.is_(None),
        Chunk.embedding_model != identity,
        Chunk.embedding_version != PROMPT_SCHEMA_VERSION,
    )


def backfill(batch_size: int, dry_run: bool) -> int:
    provider = get_embedding_provider()
    if provider is None:
        raise EmbeddingError("Backfill requires EMBEDDING_PROVIDER=ollama")
    with SessionLocal() as db:
        locked = True
        if db.bind and db.bind.dialect.name == "postgresql":
            locked = bool(db.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": ADVISORY_LOCK_ID}))
        if not locked:
            raise EmbeddingError("Another embedding backfill is already running")
        try:
            identity = provider.resolve_identity().value
            total = db.scalar(select(func.count()).select_from(Chunk)) or 0
            pending = db.scalar(select(func.count()).select_from(Chunk).where(pending_clause(identity))) or 0
            print(f"embedding_model={identity} total={total} pending={pending}")
            if dry_run:
                return 0 if pending == 0 else 2

            processed = 0
            while True:
                rows = db.execute(
                    select(Chunk, Page.title)
                    .join(Page, Chunk.page_id == Page.id)
                    .where(pending_clause(identity))
                    .order_by(Chunk.id)
                    .limit(batch_size)
                ).all()
                if not rows:
                    break
                snapshots = [(chunk.id, chunk.page_id, chunk.content, title) for chunk, title in rows]
                vectors = provider.embed_documents([(title, content) for _id, _page_id, content, title in snapshots])
                # Provider inference can take seconds. Discard the pre-call identity
                # map so the compare-and-set checks observe concurrent edits.
                db.expire_all()
                updated = 0
                for (chunk_id, page_id, original_content, original_title), vector in zip(snapshots, vectors, strict=True):
                    # Lock both inputs used to form the embedding until commit.
                    # This closes the check/flush race with API edits on Postgres.
                    page = db.scalar(select(Page).where(Page.id == page_id).with_for_update())
                    chunk = db.scalar(select(Chunk).where(Chunk.id == chunk_id).with_for_update())
                    if (
                        chunk is None
                        or page is None
                        or chunk.content != original_content
                        or page.title != original_title
                    ):
                        continue
                    chunk.embedding = vector
                    chunk.embedding_model = identity
                    chunk.embedding_version = PROMPT_SCHEMA_VERSION
                    chunk.embedding_updated_at = datetime.now(timezone.utc)
                    updated += 1
                db.commit()
                processed += updated
                print(f"processed={processed} batch_updated={updated}")
                if updated == 0:
                    raise EmbeddingError("Backfill made no progress because chunks changed concurrently")

            remaining = db.scalar(select(func.count()).select_from(Chunk).where(pending_clause(identity))) or 0
            print(f"complete processed={processed} remaining={remaining}")
            return 0 if remaining == 0 else 2
        finally:
            if db.bind and db.bind.dialect.name == "postgresql" and locked:
                db.rollback()
                db.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": ADVISORY_LOCK_ID})
            provider.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subcommands = parser.add_subparsers(dest="command", required=True)
    command = subcommands.add_parser("embeddings-backfill")
    command.add_argument("--batch-size", type=int, default=32)
    command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "embeddings-backfill":
        if args.batch_size < 1 or args.batch_size > 256:
            parser.error("--batch-size must be between 1 and 256")
        try:
            result = backfill(args.batch_size, args.dry_run)
        except EmbeddingError as exc:
            parser.exit(1, f"embedding backfill failed: {exc}\n")
        raise SystemExit(result)


if __name__ == "__main__":
    main()
