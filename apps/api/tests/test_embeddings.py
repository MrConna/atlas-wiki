import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app import embeddings
from app.embeddings import EmbeddingError, OllamaEmbeddingProvider, PROMPT_SCHEMA_VERSION, _normalize


def test_ollama_embedding_provider_batches_formats_and_normalizes(monkeypatch):
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "embeddinggemma:300m-qat-q4_0", "digest": "sha256:abc"}]})
        payload = __import__("json").loads(request.content)
        return httpx.Response(200, json={"model": payload["model"], "embeddings": [[2.0] + [0.0] * 767 for _ in payload["input"]]})

    monkeypatch.setattr("app.embeddings.settings.embedding_batch_size", 1)
    provider = OllamaEmbeddingProvider(transport=httpx.MockTransport(handler))
    identity = provider.resolve_identity()
    vectors = provider.embed_documents([("Title", "first"), ("Other", "second")])
    query = provider.embed_query("where is it")
    provider.close()

    assert identity.value == f"ollama:embeddinggemma:300m-qat-q4_0@sha256:abc:dim768:{PROMPT_SCHEMA_VERSION}"
    embed_payloads = [__import__("json").loads(request.content) for request in requests if request.url.path == "/api/embed"]
    assert embed_payloads[0]["input"] == ["title: Title | text: first"]
    assert embed_payloads[1]["input"] == ["title: Other | text: second"]
    assert embed_payloads[2]["input"] == ["task: search result | query: where is it"]
    assert all(payload["truncate"] is False and payload["dimensions"] == 768 for payload in embed_payloads)
    assert len(vectors) == 2 and len(query) == 768
    assert math.isclose(sum(value * value for value in query), 1.0)


@pytest.mark.parametrize(
    "vector",
    [
        [1.0] * 767,
        [0.0] * 768,
        [True] + [0.0] * 767,
    ],
)
def test_embedding_provider_rejects_invalid_vectors(vector):
    def handler(_request: httpx.Request):
        return httpx.Response(200, json={"model": "embeddinggemma", "embeddings": [vector]})

    provider = OllamaEmbeddingProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(EmbeddingError):
        provider.embed_query("query")
    provider.close()


def test_embedding_normalization_rejects_non_finite_values():
    with pytest.raises(EmbeddingError):
        _normalize([float("nan")] + [0.0] * 767)


def test_embedding_provider_rejects_wrong_response_count():
    provider = OllamaEmbeddingProvider(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"embeddings": []}))
    )
    with pytest.raises(EmbeddingError):
        provider.embed_query("query")
    provider.close()


def test_embedding_provider_retries_only_transient_failures(monkeypatch):
    calls = 0

    def handler(_request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * 767]})

    monkeypatch.setattr("app.embeddings.time.sleep", lambda _seconds: None)
    provider = OllamaEmbeddingProvider(transport=httpx.MockTransport(handler))
    assert len(provider.embed_query("query")) == 768
    assert calls == 3
    provider.close()


def test_embedding_provider_does_not_leak_response_body_on_error():
    provider = OllamaEmbeddingProvider(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(400, text="private document text must not leak")
        )
    )
    with pytest.raises(EmbeddingError) as error:
        provider.embed_query("query")
    assert "private document" not in str(error.value)
    provider.close()


def test_embedding_provider_bounds_concurrent_requests(monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()

    def handler(_request: httpx.Request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * 767]})

    monkeypatch.setattr("app.embeddings.settings.embedding_max_concurrency", 2)
    provider = OllamaEmbeddingProvider(transport=httpx.MockTransport(handler))
    with ThreadPoolExecutor(max_workers=6) as executor:
        vectors = list(executor.map(provider.embed_query, [f"query-{index}" for index in range(6)]))
    provider.close()

    assert len(vectors) == 6
    assert peak == 2


def test_lifecycle_provider_is_singleton_and_closes_once(monkeypatch):
    created = []

    class FakeProvider:
        _closed = False

        def __init__(self):
            self.close_calls = 0
            created.append(self)

        def close(self):
            self.close_calls += 1
            self._closed = True

    embeddings.close_embedding_provider()
    monkeypatch.setattr(embeddings.settings, "embedding_provider", "ollama")
    monkeypatch.setattr(embeddings, "OllamaEmbeddingProvider", FakeProvider)
    first = embeddings.initialize_embedding_provider()
    second = embeddings.get_embedding_provider()
    embeddings.close_embedding_provider()
    embeddings.close_embedding_provider()

    assert first is second
    assert len(created) == 1
    assert created[0].close_calls == 1
