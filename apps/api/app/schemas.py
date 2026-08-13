from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    content: str = ""


class PageUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    content: str | None = None


class PageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    slug: str
    content: str
    source_type: str
    created_at: datetime
    updated_at: datetime


class SearchResult(BaseModel):
    page_id: str
    chunk_id: str
    title: str
    excerpt: str
    score: float
    source_location: str | None = None
    heading_path: str | None = None


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    page_id: str
    filename: str
    media_type: str
    content_hash: str
    size_bytes: int
    status: str
    created_at: datetime


class ImportResponse(BaseModel):
    document: DocumentRead
    page: PageRead
    chunks_created: int
    duplicate: bool = False


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    limit: int = Field(default=8, ge=1, le=20)


class Citation(BaseModel):
    page_id: str
    chunk_id: str
    title: str
    excerpt: str
    source_location: str | None = None
    heading_path: str | None = None


class PageLinks(BaseModel):
    outbound: list[PageRead]
    backlinks: list[PageRead]


class AskResponse(BaseModel):
    answer: str
    evidence: Literal["sufficient", "insufficient", "provider_required"]
    citations: list[Citation]
