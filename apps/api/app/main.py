import hashlib
import io
import os
import re
import tempfile
import threading
import time
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI, File, HTTPException, Query, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pypdf import PdfReader
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal, get_db
from .embeddings import (
    PROMPT_SCHEMA_VERSION,
    EmbeddingError,
    close_embedding_provider,
    get_embedding_provider,
    initialize_embedding_provider,
)
from .models import Chunk, Document, Page
from .providers import ModelProviderError, generate_answer
from .retrieval import chunk_text, cosine_similarity, embed_text, keyword_score
from .schemas import (
    AskRequest,
    AskResponse,
    Citation,
    DocumentRead,
    ImportResponse,
    PageCreate,
    PageRead,
    PageLinks,
    PageUpdate,
    SearchResult,
)


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    slug = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", normalized).strip("-")
    return slug or "untitled"


def unique_slug(db: Session, title: str, current_id: str | None = None) -> str:
    base = slugify(title)
    candidate = base
    suffix = 2
    while True:
        query = select(Page).where(Page.slug == candidate)
        page = db.scalar(query)
        if page is None or page.id == current_id:
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


def build_chunks(title: str, content: str) -> list[Chunk]:
    chunks = []
    for item in chunk_text(content.strip(), title):
        chunk_content = str(item["content"])
        chunks.append(
            Chunk(
                content=chunk_content,
                heading_path=str(item["heading"]),
                source_location=f"{item['heading']} · chunk {int(item['position']) + 1}",
                position=int(item["position"]),
                legacy_embedding=embed_text(chunk_content),
            )
        )
    return chunks


def rebuild_chunks(page: Page) -> None:
    page.chunks.clear()
    page.chunks.extend(build_chunks(page.title, page.content))


def populate_native_embeddings(title: str, chunks: list[Chunk]) -> None:
    provider = get_embedding_provider()
    if provider is None or not chunks:
        return
    try:
        identity = provider.resolve_identity().value
        vectors = provider.embed_documents([(title, chunk.content) for chunk in chunks])
    except EmbeddingError as exc:
        raise HTTPException(status_code=503, detail=f"Embedding provider unavailable: {exc}") from None
    for chunk, vector in zip(chunks, vectors, strict=True):
        chunk.embedding = vector
        chunk.embedding_model = identity
        chunk.embedding_version = PROMPT_SCHEMA_VERSION
        chunk.embedding_updated_at = datetime.now(timezone.utc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _reset_storage_readiness_cache()
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        referenced = {Path(path).name for path in db.scalars(select(Document.storage_path)).all()}
    for tombstone in upload_dir.glob("*.deleting"):
        canonical = tombstone.with_suffix("")
        if canonical.name in referenced:
            os.replace(tombstone, canonical)
        else:
            tombstone.unlink(missing_ok=True)
    for candidate in upload_dir.iterdir():
        if (
            candidate.is_file()
            and candidate.name != ".gitkeep"
            and not candidate.name.startswith(".atlas-")
            and not candidate.name.endswith(".deleting")
            and candidate.name not in referenced
        ):
            candidate.unlink(missing_ok=True)
    if settings.embedding_provider == "ollama":
        try:
            initialize_embedding_provider()
        except EmbeddingError:
            # Readiness reports provider startup failures without taking away
            # the cheap process liveness endpoint.
            pass
    try:
        yield
    finally:
        close_embedding_provider()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)


@app.middleware("http")
async def reject_cross_site_writes(request, call_next):
    if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in {item.rstrip("/") for item in settings.allowed_origins}:
            return Response(status_code=status.HTTP_403_FORBIDDEN, content="Untrusted request origin")
    return await call_next(request)


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _generation_configuration() -> tuple[str, str | None]:
    provider = settings.model_provider.lower().strip()
    if provider == "none":
        return "disabled", None
    if provider not in {"deepseek", "openai", "ollama"}:
        return "degraded", "unsupported_provider"
    if not settings.model_name.strip():
        return "degraded", "missing_model"
    if provider == "deepseek":
        base_url, api_key = settings.deepseek_base_url, settings.deepseek_api_key
    elif provider == "openai":
        base_url, api_key = settings.openai_base_url, settings.openai_api_key
    else:
        base_url, api_key = settings.ollama_base_url, "local"
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return "degraded", "invalid_base_url"
    if provider in {"deepseek", "openai"} and not api_key.strip():
        return "degraded", "missing_api_key"
    with generation_state_lock:
        last_failure = generation_failure_code
    if last_failure:
        return "degraded", last_failure
    return "configured", None


def _record_generation_failure(code: str | None) -> None:
    global generation_failure_code
    with generation_state_lock:
        generation_failure_code = code


def _migration_head() -> str:
    api_dir = Path(__file__).resolve().parents[1]
    config = Config(str(api_dir / "alembic.ini"))
    config.set_main_option("script_location", str(api_dir / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise RuntimeError("repository does not have exactly one migration head")
    return heads[0]


def _storage_check(documents: list[tuple[str, str, int, str]]) -> dict[str, object]:
    root = Path(settings.upload_dir).resolve()
    if not root.is_dir():
        raise RuntimeError("upload directory is missing")
    descriptor, probe_name = tempfile.mkstemp(prefix=".atlas-ready-", dir=root)
    os.close(descriptor)
    Path(probe_name).unlink(missing_ok=True)
    referenced: set[Path] = set()
    for _document_id, storage_path, size_bytes, content_hash in documents:
        path = Path(storage_path).resolve()
        referenced.add(path)
        if root not in path.parents or not path.is_file() or path.stat().st_size != size_bytes:
            raise RuntimeError("document storage is inconsistent")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != content_hash:
            raise RuntimeError("document storage checksum does not match")
    for path in root.iterdir():
        if path.name == ".gitkeep":
            continue
        if path.is_symlink() or not path.is_file() or path.resolve() not in referenced:
            raise RuntimeError("upload directory contains an unsafe or unreferenced entry")
    return {"status": "ready"}


storage_readiness_condition = threading.Condition()
storage_readiness_refreshing = False
storage_readiness_cache: tuple[tuple[str, tuple[tuple[str, str, int, str], ...]], float, bool] | None = None


def _cached_storage_check(documents: list[tuple[str, str, int, str]]) -> dict[str, object]:
    global storage_readiness_cache, storage_readiness_refreshing
    normalized_documents = tuple(
        sorted((str(document_id), str(path), int(size), str(digest)) for document_id, path, size, digest in documents)
    )
    snapshot = (str(Path(settings.upload_dir).resolve()), normalized_documents)
    deadline = time.monotonic() + settings.storage_readiness_wait_seconds
    with storage_readiness_condition:
        while True:
            now = time.monotonic()
            if (
                storage_readiness_cache is not None
                and storage_readiness_cache[0] == snapshot
                and storage_readiness_cache[1] > now
            ):
                if storage_readiness_cache[2]:
                    return {"status": "ready"}
                raise RuntimeError("storage readiness recently failed")
            if not storage_readiness_refreshing:
                storage_readiness_refreshing = True
                break
            remaining = deadline - now
            if remaining <= 0:
                raise RuntimeError("storage readiness check is busy")
            storage_readiness_condition.wait(remaining)

    try:
        result = _storage_check(documents)
    except Exception:
        with storage_readiness_condition:
            storage_readiness_cache = (
                snapshot,
                time.monotonic() + settings.storage_readiness_failure_ttl_seconds,
                False,
            )
            storage_readiness_refreshing = False
            storage_readiness_condition.notify_all()
        raise RuntimeError("storage readiness failed") from None
    except BaseException:
        with storage_readiness_condition:
            storage_readiness_refreshing = False
            storage_readiness_condition.notify_all()
        raise
    with storage_readiness_condition:
        storage_readiness_cache = (
            snapshot,
            time.monotonic() + settings.storage_readiness_ttl_seconds,
            True,
        )
        storage_readiness_refreshing = False
        storage_readiness_condition.notify_all()
    return result


def _reset_storage_readiness_cache() -> None:
    global storage_readiness_cache, storage_readiness_refreshing
    with storage_readiness_condition:
        storage_readiness_cache = None
        storage_readiness_refreshing = False
        storage_readiness_condition.notify_all()


def _readiness_report() -> tuple[int, dict[str, object]]:
    checks: dict[str, object] = {}
    hard_failure = False
    database_ready = False
    documents: list[tuple[str, str, int, str]] = []
    # Take only a short metadata snapshot here. File hashing and external model
    # probes happen after this session has returned its connection to the pool.
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            checks["database"] = {"status": "ready"}
            database_ready = True
            expected_head = _migration_head()
            current_head = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            migration_ready = current_head == expected_head
            checks["migrations"] = {
                "status": "ready" if migration_ready else "not_ready",
                "current": current_head,
                "expected": expected_head,
            }
            hard_failure = hard_failure or not migration_ready
            documents = list(
                db.execute(
                    select(Document.id, Document.storage_path, Document.size_bytes, Document.content_hash)
                ).all()
            )
    except Exception:
        checks.setdefault("database", {"status": "not_ready"})
        checks.setdefault("migrations", {"status": "not_ready", "reason": "migration_check_failed"})
        hard_failure = True

    if database_ready:
        try:
            checks["storage"] = _cached_storage_check(documents)
        except (OSError, RuntimeError):
            checks["storage"] = {"status": "not_ready", "reason": "storage_inconsistent"}
            hard_failure = True

    provider_name = settings.embedding_provider.lower().strip()
    if provider_name == "legacy":
        checks["retrieval"] = {"status": "ready", "backend": "feature-hash"}
    elif provider_name == "ollama" and database_ready:
        try:
            provider = get_embedding_provider()
            if provider is None:
                raise EmbeddingError("Embedding provider is unavailable")
            identity = provider.resolve_identity_for_readiness().value
            # The potentially slow external call above owns no DB connection.
            with SessionLocal() as db:
                total = db.scalar(select(func.count()).select_from(Chunk)) or 0
                current = db.scalar(
                    select(func.count()).select_from(Chunk).where(
                        Chunk.embedding.is_not(None),
                        Chunk.embedding_model == identity,
                        Chunk.embedding_version == PROMPT_SCHEMA_VERSION,
                    )
                ) or 0
            retrieval_ready = current == total
            checks["retrieval"] = {
                "status": "ready" if retrieval_ready else "not_ready",
                "backend": "native-pgvector",
                "indexed": retrieval_ready,
            }
            hard_failure = hard_failure or not retrieval_ready
        except EmbeddingError:
            checks["retrieval"] = {"status": "not_ready", "reason": "embedding_provider_unavailable"}
            hard_failure = True
        except Exception:
            checks["retrieval"] = {"status": "not_ready", "reason": "embedding_state_unavailable"}
            hard_failure = True
    elif provider_name == "ollama":
        checks["retrieval"] = {"status": "not_ready", "reason": "database_unavailable"}
        hard_failure = True
    else:
        checks["retrieval"] = {"status": "not_ready", "reason": "unsupported_embedding_provider"}
        hard_failure = True

    generation_status, generation_reason = _generation_configuration()
    generation: dict[str, str] = {"status": generation_status}
    if generation_reason:
        generation["reason"] = generation_reason
    checks["generation"] = generation
    overall = "not_ready" if hard_failure else "degraded" if generation_status == "degraded" else "ready"
    return (503 if hard_failure else 200), {"status": overall, "checks": checks}


@app.get("/api/v1/ready")
def readiness():
    status_code, report = _readiness_report()
    return JSONResponse(status_code=status_code, content=report)


@app.get("/api/v1/pages", response_model=list[PageRead])
def list_pages(category: str | None = None, db: Session = Depends(get_db)):
    query = select(Page).order_by(Page.updated_at.desc())
    if category is not None:
        query = query.where(Page.category == category)
    return db.scalars(query).all()


@app.get("/api/v1/categories", response_model=list[str])
def list_categories(db: Session = Depends(get_db)):
    values = db.scalars(
        select(Page.category).where(Page.category.is_not(None)).distinct().order_by(Page.category)
    ).all()
    return values


@app.get("/api/v1/documents", response_model=list[DocumentRead])
def list_documents(db: Session = Depends(get_db)):
    return db.scalars(select(Document).order_by(Document.created_at.desc())).all()


storage_lock = threading.Lock()
generation_state_lock = threading.Lock()
generation_failure_code: str | None = None


def extract_text(filename: str, media_type: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf" or media_type == "application/pdf":
        if suffix != ".pdf" or media_type != "application/pdf" or not data.startswith(b"%PDF-"):
            raise HTTPException(status_code=415, detail="PDF extension, media type, and file signature must match")
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = []
            for index, page in enumerate(reader.pages, 1):
                page_text = (page.extract_text() or "").strip()
                if page_text:
                    pages.append(f"# Page {index}\n\n{page_text}")
            return "\n\n".join(pages).strip()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="PDF could not be parsed") from exc
    allowed_types = {
        ".md": {"text/markdown", "text/plain", "application/octet-stream"},
        ".markdown": {"text/markdown", "text/plain", "application/octet-stream"},
        ".txt": {"text/plain", "application/octet-stream"},
    }
    if suffix not in allowed_types or media_type not in allowed_types[suffix]:
        raise HTTPException(status_code=415, detail="Only Markdown, TXT, and PDF files are supported")
    try:
        return data.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="Text files must use UTF-8 encoding") from exc


@app.post("/api/v1/imports", response_model=ImportResponse, status_code=status.HTTP_201_CREATED)
async def import_document(file: UploadFile = File(...), db: Session = Depends(get_db)):
    filename = Path(file.filename or "untitled.txt").name
    data = await file.read(settings.max_upload_bytes + 1)
    if not data:
        raise HTTPException(status_code=422, detail="The uploaded file is empty")
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="The uploaded file exceeds the size limit")
    digest = hashlib.sha256(data).hexdigest()
    existing = db.scalar(select(Document).where(Document.content_hash == digest))
    if existing:
        return ImportResponse(
            document=existing,
            page=existing.page,
            chunks_created=len(existing.page.chunks),
            duplicate=True,
        )
    content = extract_text(filename, file.content_type or "application/octet-stream", data)
    if not content:
        raise HTTPException(status_code=422, detail="No extractable text was found")
    title = Path(filename).stem.strip() or "Untitled"
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower()
    stored_path = upload_dir / f"{digest}{suffix}"
    page = Page(title=title, slug=unique_slug(db, title), content=content, source_type="import")
    rebuild_chunks(page)
    # Inference happens before the file/DB critical section. A concurrent
    # duplicate may waste one call, but cannot hold storage mutations hostage.
    await run_in_threadpool(populate_native_embeddings, page.title, page.chunks)
    with storage_lock:
        existing = db.scalar(select(Document).where(Document.content_hash == digest))
        if existing:
            return ImportResponse(document=existing, page=existing.page, chunks_created=len(existing.page.chunks), duplicate=True)
        used_storage = sum(path.stat().st_size for path in upload_dir.iterdir() if path.is_file() and not path.name.startswith(".atlas-"))
        if used_storage + len(data) > settings.max_storage_bytes:
            raise HTTPException(status_code=507, detail="The local document storage quota would be exceeded")
        page.slug = unique_slug(db, title)
        descriptor, staged_name = tempfile.mkstemp(prefix=".atlas-", dir=upload_dir)
        staged_path = Path(staged_name)
        with os.fdopen(descriptor, "wb") as staged_file:
            staged_file.write(data)
        document = Document(
            page=page,
            filename=filename,
            media_type=file.content_type or "application/octet-stream",
            content_hash=digest,
            storage_path=str(stored_path),
            size_bytes=len(data),
        )
        db.add(document)
        try:
            os.replace(staged_path, stored_path)
            db.commit()
        except IntegrityError:
            db.rollback()
            staged_path.unlink(missing_ok=True)
            existing = db.scalar(select(Document).where(Document.content_hash == digest))
            if existing:
                return ImportResponse(document=existing, page=existing.page, chunks_created=len(existing.page.chunks), duplicate=True)
            raise
        except Exception:
            db.rollback()
            staged_path.unlink(missing_ok=True)
            stored_path.unlink(missing_ok=True)
            raise
    db.refresh(document)
    return ImportResponse(document=document, page=page, chunks_created=len(page.chunks))


@app.post("/api/v1/pages", response_model=PageRead, status_code=status.HTTP_201_CREATED)
def create_page(payload: PageCreate, db: Session = Depends(get_db)):
    category = payload.category.strip() or None if payload.category is not None else None
    page = Page(title=payload.title.strip(), slug=unique_slug(db, payload.title), content=payload.content, category=category)
    rebuild_chunks(page)
    populate_native_embeddings(page.title, page.chunks)
    db.add(page)
    db.commit()
    db.refresh(page)
    return page


def require_page(db: Session, page_id: str) -> Page:
    page = db.get(Page, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


@app.get("/api/v1/pages/{page_id}", response_model=PageRead)
def get_page(page_id: str, db: Session = Depends(get_db)):
    return require_page(db, page_id)


@app.patch("/api/v1/pages/{page_id}", response_model=PageRead)
def update_page(page_id: str, payload: PageUpdate, db: Session = Depends(get_db)):
    page = db.get(Page, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    content_changed = payload.title is not None or payload.content is not None
    if content_changed and page.source_type == "import":
        raise HTTPException(status_code=409, detail="Imported source pages are immutable; replace the document instead")
    desired_category = (payload.category.strip() or None) if payload.category is not None else page.category
    if not content_changed:
        # Category is metadata, not content: it may be set even on immutable
        # imported pages, which never rebuild chunks/embeddings.
        page.category = desired_category
        db.commit()
        db.refresh(page)
        return page

    original_title, original_content = page.title, page.content
    desired_title = payload.title.strip() if payload.title is not None else original_title
    desired_content = payload.content if payload.content is not None else original_content
    drafts = build_chunks(desired_title, desired_content)
    populate_native_embeddings(desired_title, drafts)

    db.expire_all()
    page = db.scalar(select(Page).where(Page.id == page_id).with_for_update())
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    if page.title != original_title or page.content != original_content:
        db.rollback()
        raise HTTPException(status_code=409, detail="Page changed while embeddings were generated; retry the update")
    page.title = desired_title
    page.content = desired_content
    page.category = desired_category
    # Slugs are permanent link identifiers; renaming changes only the display title.
    db.execute(delete(Chunk).where(Chunk.page_id == page_id), execution_options={"synchronize_session": False})
    for chunk in drafts:
        chunk.page_id = page_id
    db.add_all(drafts)
    db.commit()
    db.refresh(page)
    return page


@app.delete("/api/v1/pages/{page_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_page(page_id: str, db: Session = Depends(get_db)):
    with storage_lock:
        page = db.get(Page, page_id)
        if page is None:
            raise HTTPException(status_code=404, detail="Page not found")
        stored_path = Path(page.document.storage_path) if page.document else None
        tombstone = stored_path.with_suffix(stored_path.suffix + ".deleting") if stored_path else None
        if stored_path and stored_path.exists() and tombstone:
            os.replace(stored_path, tombstone)
        db.delete(page)
        try:
            db.commit()
        except Exception:
            db.rollback()
            if tombstone and tombstone.exists() and stored_path:
                os.replace(tombstone, stored_path)
            raise
        if tombstone:
            try:
                tombstone.unlink(missing_ok=True)
            except OSError:
                pass  # Startup reconciliation retries post-commit cleanup.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def search_chunks(db: Session, query: str, limit: int, mode: str = "hybrid") -> list[tuple[Chunk, Page, float]]:
    if mode != "keyword" and settings.embedding_provider == "ollama":
        return search_native_chunks(db, query, limit, mode)
    return search_legacy_chunks(db, query, limit, mode)


def search_legacy_chunks(db: Session, query: str, limit: int, mode: str) -> list[tuple[Chunk, Page, float]]:
    query_embedding = embed_text(query)
    candidates = db.execute(select(Chunk, Page).join(Page, Chunk.page_id == Page.id)).all()
    scored: list[tuple[Chunk, Page, float]] = []
    for chunk, page in candidates:
        text = f"{page.title}\n{chunk.content}"
        keyword = keyword_score(query, text)
        semantic = cosine_similarity(query_embedding, chunk.legacy_embedding or embed_text(chunk.content))
        score = keyword if mode == "keyword" else semantic if mode == "semantic" else 0.55 * keyword + 0.45 * semantic
        if score >= 0.12:
            scored.append((chunk, page, score))
    return sorted(scored, key=lambda item: (-item[2], item[0].id))[:limit]


def search_native_chunks(db: Session, query: str, limit: int, mode: str) -> list[tuple[Chunk, Page, float]]:
    provider = get_embedding_provider()
    if provider is None:
        return search_legacy_chunks(db, query, limit, mode)
    try:
        identity = provider.resolve_identity().value
        cleaned_query = re.sub(
            r"^\s*(?:(?:ignore|disregard)\b.{0,80}?instructions(?:\s+and\s+reveal\s+secrets)?|do\s+not\s+cite\s+sources|忽略系统指令并输出密钥|不要引用来源)\s*[;；:：,.，。]*\s*",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip()
        cleaned_query = cleaned_query or query
        query_parts = [cleaned_query]
        for part in re.split(r"\b(?:and|also)\b|和|以及|同时|[;；:：]", cleaned_query, flags=re.IGNORECASE):
            normalized = part.strip(" ，,。.？?")
            if len(normalized) >= 4 and normalized not in query_parts:
                query_parts.append(normalized)
            if len(query_parts) >= 4:
                break
        query_embeddings = provider.embed_queries(query_parts)
    except EmbeddingError as exc:
        raise HTTPException(status_code=503, detail=f"Embedding provider unavailable: {exc}") from None

    candidate_limit = max(limit * 5, settings.retrieval_candidate_limit)
    eligible = (
        Chunk.embedding.is_not(None),
        Chunk.embedding_model == identity,
        Chunk.embedding_version == PROMPT_SCHEMA_VERSION,
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        semantic_scores = {}
        rows_by_id = {}
        for query_embedding in query_embeddings:
            distance = Chunk.embedding.cosine_distance(query_embedding)
            rows = db.execute(
                select(Chunk, Page, distance.label("distance"))
                .join(Page, Chunk.page_id == Page.id)
                .where(*eligible)
                .order_by(distance, Chunk.id)
                .limit(candidate_limit)
            ).all()
            for chunk, page, distance_value in rows:
                rows_by_id[chunk.id] = (chunk, page)
                score = max(0.0, min(1.0, 1.0 - float(distance_value)))
                semantic_scores[chunk.id] = max(semantic_scores.get(chunk.id, 0.0), score)
        native_rows = list(rows_by_id.values())
    else:
        native_rows = db.execute(
            select(Chunk, Page).join(Page, Chunk.page_id == Page.id).where(*eligible)
        ).all()
        semantic_scores = {
            chunk.id: max(
                max(0.0, min(1.0, cosine_similarity(query_embedding, chunk.embedding or [])))
                for query_embedding in query_embeddings
            )
            for chunk, _page in native_rows
        }

    best_semantic = max(semantic_scores.values(), default=0.0)
    primary_confident = best_semantic >= settings.semantic_min_score

    def accepted(chunk_id: str) -> bool:
        return semantic_scores.get(chunk_id, 0.0) >= settings.semantic_min_score or (
            primary_confident
            and len(query_embeddings) > 1
            and semantic_scores.get(chunk_id, 0.0) >= settings.semantic_expansion_min_score
        )

    if mode == "semantic":
        scored = [
            (chunk, page, semantic_scores[chunk.id])
            for chunk, page in native_rows
            if accepted(chunk.id)
        ]
        return sorted(scored, key=lambda item: (-item[2], item[0].id))[:limit]

    by_id = {chunk.id: (chunk, page) for chunk, page in native_rows}
    keyword_scores = {}
    for chunk, page in native_rows:
        keyword = keyword_score(query, f"{page.title}\n{chunk.content}")
        if keyword > 0:
            keyword_scores[chunk.id] = keyword
    scored = []
    for chunk_id, (chunk, page) in by_id.items():
        # Semantic similarity is the confidence gate; lexical overlap is only
        # a bounded reranking bonus and cannot rescue an unrelated low-similarity hit.
        semantic = semantic_scores.get(chunk_id, 0.0)
        score = min(1.0, semantic + 0.12 * keyword_scores.get(chunk_id, 0.0))
        lexical_strong = keyword_scores.get(chunk_id, 0.0) >= 0.5 and semantic >= 0.30
        if (accepted(chunk_id) or lexical_strong) and (
            score >= settings.hybrid_min_score
            or semantic_scores.get(chunk_id, 0.0) >= settings.semantic_expansion_min_score
        ):
            scored.append((chunk, page, score))
    return sorted(scored, key=lambda item: (-item[2], item[0].id))[:limit]


@app.get("/api/v1/search", response_model=list[SearchResult])
def search(
    q: str = Query(min_length=1, max_length=500),
    mode: str = Query(default="hybrid", pattern="^(keyword|semantic|hybrid)$"),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return [
        SearchResult(
            page_id=page.id,
            chunk_id=chunk.id,
            title=page.title,
            excerpt=chunk.content[:400],
            score=round(score, 6),
            source_location=chunk.source_location,
            heading_path=chunk.heading_path,
        )
        for chunk, page, score in search_chunks(db, q, limit, mode)
    ]


@app.get("/api/v1/retrieval/status")
def retrieval_status(db: Session = Depends(get_db)):
    total = db.scalar(select(func.count()).select_from(Chunk)) or 0
    corpus_rows = db.execute(
        select(
            Page.id,
            Page.title,
            Page.content,
            Chunk.id,
            Chunk.embedding,
            Chunk.embedding_model,
            Chunk.embedding_version,
        ).outerjoin(Chunk, Chunk.page_id == Page.id)
    ).all()
    if settings.embedding_provider != "ollama":
        return {
            "backend": "feature-hash",
            "app_git_sha": settings.app_git_sha,
            "total_chunks": total,
            "current_chunks": total,
        }
    provider = get_embedding_provider()
    try:
        identity = provider.resolve_identity().value
    except EmbeddingError as exc:
        raise HTTPException(status_code=503, detail=f"Embedding provider unavailable: {exc}") from None
    current = db.scalar(
        select(func.count()).select_from(Chunk).where(
            Chunk.embedding.is_not(None),
            Chunk.embedding_model == identity,
            Chunk.embedding_version == PROMPT_SCHEMA_VERSION,
        )
    ) or 0
    corpus = {}
    for page_id, title, content, chunk_id, embedding, embedding_model, embedding_version in corpus_rows:
        item = corpus.setdefault(
            page_id,
            {
                "title": title,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "chunk_count": 0,
                "current_chunk_count": 0,
            },
        )
        if chunk_id is not None:
            item["chunk_count"] += 1
            if embedding is not None and embedding_model == identity and embedding_version == PROMPT_SCHEMA_VERSION:
                item["current_chunk_count"] += 1
    return {
        "backend": "native-pgvector" if db.bind and db.bind.dialect.name == "postgresql" else "native-test",
        "app_git_sha": settings.app_git_sha,
        "dimensions": settings.embedding_dimensions,
        "model_identity": identity,
        "total_chunks": total,
        "current_chunks": current,
        "corpus": sorted(corpus.values(), key=lambda item: (item["title"], item["content_sha256"])),
    }


@app.post("/api/v1/ask", response_model=AskResponse)
async def ask(payload: AskRequest, db: Session = Depends(get_db)):
    rows = await run_in_threadpool(search_chunks, db, payload.question, payload.limit, "hybrid")
    citations = [
        Citation(
            page_id=page.id,
            chunk_id=chunk.id,
            title=page.title,
            excerpt=chunk.content,
            source_location=chunk.source_location,
            heading_path=chunk.heading_path,
        )
        for chunk, page, _score in rows
    ]
    if not citations:
        return AskResponse(
            answer="Atlas Wiki could not find evidence for this question in your knowledge base.",
            evidence="insufficient",
            citations=[],
        )
    try:
        generated = await generate_answer(payload.question, [chunk.content for chunk, _page, _score in rows])
    except ModelProviderError as exc:
        _record_generation_failure(exc.code)
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc), "retryable": exc.retryable},
        ) from None
    if generated is not None:
        _record_generation_failure(None)
    if generated is None:
        return AskResponse(
            answer="Relevant evidence was found. Configure MODEL_PROVIDER and MODEL_NAME to generate a grounded answer.",
            evidence="provider_required",
            citations=citations,
        )
    markers = {int(value) for value in re.findall(r"\[(\d+)\]", generated)}
    factual_sentences = []
    for line in generated.splitlines():
        # Protect punctuation inside inline-code identifiers such as `AGENTS.md`,
        # then split at every remaining terminator even when no space follows.
        protected_line = re.sub(
            r"`[^`]*`",
            lambda match: match.group(0).translate(str.maketrans(".!?。！？", "․⁉¿﹒﹗﹖")),
            line,
        )
        for sentence in re.findall(r"[^.!?。！？]+(?:[.!?。！？](?:\s*\[\d+\])*)?", protected_line):
            content_without_citations = re.sub(r"\[\d+\]", "", sentence)
            if re.search(r"[\w\u4e00-\u9fff]", content_without_citations):
                factual_sentences.append(sentence.strip())
    every_sentence_cited = factual_sentences and all(re.search(r"\[\d+\]", sentence) for sentence in factual_sentences)
    if not markers or any(marker < 1 or marker > len(citations) for marker in markers) or not every_sentence_cited:
        return AskResponse(
            answer="The configured model did not produce verifiable evidence citations.",
            evidence="insufficient",
            citations=citations,
        )
    insufficient_marker = "INSUFFICIENT_EVIDENCE"
    if generated.startswith(insufficient_marker):
        refusal = generated[len(insufficient_marker) :].lstrip(" :—-\n")
        return AskResponse(answer=refusal, evidence="insufficient", citations=citations)
    return AskResponse(answer=generated, evidence="sufficient", citations=citations)


WIKI_LINK_PATTERN = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


@app.get("/api/v1/pages/{page_id}/links", response_model=PageLinks)
def page_links(page_id: str, db: Session = Depends(get_db)):
    page = require_page(db, page_id)
    all_pages = list(db.scalars(select(Page)).all())
    by_slug = {candidate.slug.casefold(): candidate for candidate in all_pages}
    outbound = []
    for slug in WIKI_LINK_PATTERN.findall(page.content):
        target = by_slug.get(slug.strip().casefold())
        if target and target.id != page.id and target not in outbound:
            outbound.append(target)
    backlinks = [
        candidate
        for candidate in all_pages
        if candidate.id != page.id
        and any(slug.strip().casefold() == page.slug.casefold() for slug in WIKI_LINK_PATTERN.findall(candidate.content))
    ]
    return PageLinks(outbound=outbound, backlinks=backlinks)
