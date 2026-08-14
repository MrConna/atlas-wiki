import hashlib
import io
import os
import re
import tempfile
import threading
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal, get_db
from .models import Chunk, Document, Page
from .providers import generate_answer
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


def rebuild_chunks(page: Page) -> None:
    page.chunks.clear()
    content = page.content.strip()
    for item in chunk_text(content, page.title):
        chunk_content = str(item["content"])
        page.chunks.append(
            Chunk(
                content=chunk_content,
                heading_path=str(item["heading"]),
                source_location=f"{item['heading']} · chunk {int(item['position']) + 1}",
                position=int(item["position"]),
                legacy_embedding=embed_text(chunk_content),
            )
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
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
    yield


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


@app.get("/api/v1/pages", response_model=list[PageRead])
def list_pages(db: Session = Depends(get_db)):
    return db.scalars(select(Page).order_by(Page.updated_at.desc())).all()


@app.get("/api/v1/documents", response_model=list[DocumentRead])
def list_documents(db: Session = Depends(get_db)):
    return db.scalars(select(Document).order_by(Document.created_at.desc())).all()


storage_lock = threading.Lock()


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
    with storage_lock:
        existing = db.scalar(select(Document).where(Document.content_hash == digest))
        if existing:
            return ImportResponse(document=existing, page=existing.page, chunks_created=len(existing.page.chunks), duplicate=True)
        used_storage = sum(path.stat().st_size for path in upload_dir.iterdir() if path.is_file() and not path.name.startswith(".atlas-"))
        if used_storage + len(data) > settings.max_storage_bytes:
            raise HTTPException(status_code=507, detail="The local document storage quota would be exceeded")
        descriptor, staged_name = tempfile.mkstemp(prefix=".atlas-", dir=upload_dir)
        staged_path = Path(staged_name)
        with os.fdopen(descriptor, "wb") as staged_file:
            staged_file.write(data)
        page = Page(title=title, slug=unique_slug(db, title), content=content, source_type="import")
        rebuild_chunks(page)
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
    page = Page(title=payload.title.strip(), slug=unique_slug(db, payload.title), content=payload.content)
    rebuild_chunks(page)
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
    page = require_page(db, page_id)
    if page.source_type == "import":
        raise HTTPException(status_code=409, detail="Imported source pages are immutable; replace the document instead")
    if payload.title is not None:
        page.title = payload.title.strip()
        # Slugs are permanent link identifiers; renaming changes only the display title.
    if payload.content is not None:
        page.content = payload.content
    rebuild_chunks(page)
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
    return sorted(scored, key=lambda item: item[2], reverse=True)[:limit]


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


@app.post("/api/v1/ask", response_model=AskResponse)
async def ask(payload: AskRequest, db: Session = Depends(get_db)):
    rows = search_chunks(db, payload.question, payload.limit, "hybrid")
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
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Model provider failed: {type(exc).__name__}") from exc
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
