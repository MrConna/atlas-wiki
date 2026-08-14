import math
import threading
import time
from dataclasses import dataclass

import httpx

from .config import settings
from .types import EMBEDDING_DIMENSIONS


PROMPT_SCHEMA_VERSION = "embeddinggemma-retrieval-v1"


class EmbeddingError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingIdentity:
    model: str
    digest: str

    @property
    def value(self) -> str:
        return f"ollama:{self.model}@{self.digest}:dim{EMBEDDING_DIMENSIONS}:{PROMPT_SCHEMA_VERSION}"


def _normalize(vector: object) -> list[float]:
    if not isinstance(vector, list) or len(vector) != settings.embedding_dimensions:
        raise EmbeddingError("Embedding provider returned an invalid vector dimension")
    values = []
    for item in vector:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise EmbeddingError("Embedding provider returned non-finite vector data")
        values.append(float(item))
    norm = math.sqrt(sum(item * item for item in values))
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingError("Embedding provider returned a zero vector")
    return [item / norm for item in values]


class OllamaEmbeddingProvider:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        if settings.embedding_dimensions != EMBEDDING_DIMENSIONS:
            raise EmbeddingError(
                f"Configured embedding dimension must be {EMBEDDING_DIMENSIONS} for the database schema"
            )
        self.base_url = settings.embedding_base_url.rstrip("/")
        self.model = settings.embedding_model
        concurrency = settings.embedding_max_concurrency
        self.client = httpx.Client(
            timeout=settings.embedding_timeout_seconds,
            limits=httpx.Limits(
                max_connections=concurrency,
                max_keepalive_connections=concurrency,
            ),
            transport=transport,
        )
        self._capacity = threading.BoundedSemaphore(concurrency)
        self._closed = False
        self._close_lock = threading.Lock()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._closed:
            raise EmbeddingError("Embedding provider is closed")
        acquired = self._capacity.acquire(timeout=settings.embedding_queue_timeout_seconds)
        if not acquired:
            raise EmbeddingError("Embedding provider is busy")
        try:
            return self._request_with_retries(method, path, **kwargs)
        finally:
            self._capacity.release()

    def _request_with_retries(self, method: str, path: str, **kwargs) -> httpx.Response:
        retry_statuses = {429, 502, 503, 504}
        attempts = 3
        for attempt in range(attempts):
            try:
                response = self.client.request(method, f"{self.base_url}{path}", **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt == attempts - 1:
                    raise EmbeddingError(f"Embedding provider unavailable ({type(exc).__name__})") from None
            else:
                if response.status_code < 400:
                    return response
                if response.status_code not in retry_statuses or attempt == attempts - 1:
                    raise EmbeddingError(f"Embedding provider request failed (HTTP {response.status_code})")
            time.sleep(0.1 * (2**attempt))
        raise EmbeddingError("Embedding provider unavailable")

    def close(self) -> None:
        with self._close_lock:
            if not self._closed:
                self._closed = True
                self.client.close()

    def resolve_identity(self) -> EmbeddingIdentity:
        response = self._request("GET", "/api/tags")
        try:
            models = response.json().get("models", [])
        except (ValueError, AttributeError):
            raise EmbeddingError("Embedding provider returned an invalid model response") from None
        for model in models:
            names = {model.get("name"), model.get("model")}
            if self.model in names:
                digest = model.get("digest")
                if not isinstance(digest, str) or not digest:
                    break
                return EmbeddingIdentity(model=self.model, digest=digest)
        raise EmbeddingError("Configured embedding model is not installed")

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        response = self._request(
            "POST",
            "/api/embed",
            json={
                "model": self.model,
                "input": inputs,
                "truncate": False,
                "dimensions": settings.embedding_dimensions,
            },
        )
        try:
            payload = response.json()
        except ValueError:
            raise EmbeddingError("Embedding provider returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise EmbeddingError("Embedding provider returned an invalid response")
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(inputs):
            raise EmbeddingError("Embedding provider returned the wrong number of vectors")
        return [_normalize(vector) for vector in embeddings]

    def embed_documents(self, items: list[tuple[str, str]]) -> list[list[float]]:
        prompts = [f"title: {title or 'none'} | text: {content}" for title, content in items]
        output = []
        for start in range(0, len(prompts), settings.embedding_batch_size):
            output.extend(self._embed(prompts[start : start + settings.embedding_batch_size]))
        return output

    def embed_query(self, query: str) -> list[float]:
        return self._embed([f"task: search result | query: {query}"])[0]

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        prompts = [f"task: search result | query: {query}" for query in queries]
        output = []
        for start in range(0, len(prompts), settings.embedding_batch_size):
            output.extend(self._embed(prompts[start : start + settings.embedding_batch_size]))
        return output


_provider: OllamaEmbeddingProvider | None = None
_provider_lock = threading.Lock()


def initialize_embedding_provider() -> OllamaEmbeddingProvider | None:
    global _provider
    if settings.embedding_provider == "legacy":
        return None
    if settings.embedding_provider != "ollama":
        raise EmbeddingError(f"Unsupported embedding provider: {settings.embedding_provider}")
    with _provider_lock:
        if _provider is None or _provider._closed:
            _provider = OllamaEmbeddingProvider()
        return _provider


def get_embedding_provider() -> OllamaEmbeddingProvider | None:
    return initialize_embedding_provider()


def close_embedding_provider() -> None:
    global _provider
    with _provider_lock:
        provider = _provider
        _provider = None
    if provider is not None:
        provider.close()
