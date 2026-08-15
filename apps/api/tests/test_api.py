from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text

from app import main as main_module
from app.database import Base, SessionLocal, engine
from app.embeddings import EmbeddingError
from app.main import app
from app.models import Chunk, Document


client = TestClient(app)


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_health():
    assert client.get("/api/v1/health").json() == {"status": "ok"}


def prepare_readiness_database(monkeypatch, tmp_path: Path):
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(128) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0003_page_category')"))
    monkeypatch.setattr("app.main.settings.upload_dir", str(tmp_path))
    monkeypatch.setattr("app.main.settings.embedding_provider", "legacy")
    monkeypatch.setattr("app.main.settings.model_provider", "none")
    monkeypatch.setattr("app.main.generation_failure_code", None)
    main_module._reset_storage_readiness_cache()


def test_readiness_reports_ready_without_optional_generation(monkeypatch, tmp_path):
    prepare_readiness_database(monkeypatch, tmp_path)
    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["checks"]["generation"]["status"] == "disabled"


def test_readiness_generation_misconfiguration_is_degraded(monkeypatch, tmp_path):
    prepare_readiness_database(monkeypatch, tmp_path)
    monkeypatch.setattr("app.main.settings.model_provider", "deepseek")
    monkeypatch.setattr("app.main.settings.model_name", "deepseek-v4-flash")
    monkeypatch.setattr("app.main.settings.deepseek_api_key", "")
    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["generation"]["reason"] == "missing_api_key"


def test_readiness_remembers_generation_failure_as_degraded(monkeypatch, tmp_path):
    prepare_readiness_database(monkeypatch, tmp_path)
    monkeypatch.setattr("app.main.settings.model_provider", "deepseek")
    monkeypatch.setattr("app.main.settings.model_name", "deepseek-v4-flash")
    monkeypatch.setattr("app.main.settings.deepseek_api_key", "configured-secret")
    monkeypatch.setattr("app.main.generation_failure_code", "provider_unavailable")
    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["generation"]["reason"] == "provider_unavailable"


def test_readiness_native_provider_failure_is_not_ready(monkeypatch, tmp_path):
    prepare_readiness_database(monkeypatch, tmp_path)
    monkeypatch.setattr("app.main.settings.embedding_provider", "ollama")

    class FailingProvider:
        def resolve_identity_for_readiness(self):
            raise EmbeddingError("private upstream failure")

    monkeypatch.setattr("app.main.get_embedding_provider", lambda: FailingProvider())
    response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["retrieval"] == {
        "status": "not_ready",
        "reason": "embedding_provider_unavailable",
    }
    assert "private upstream failure" not in response.text


def test_readiness_rejects_same_length_upload_tampering(monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.settings.upload_dir", str(tmp_path))
    monkeypatch.setattr("app.main.settings.embedding_provider", "legacy")
    imported = client.post(
        "/api/v1/imports",
        files={"file": ("source.txt", b"trusted bytes", "text/plain")},
    )
    assert imported.status_code == 201
    with SessionLocal() as db:
        document = db.scalar(select(Document))
        stored_path = Path(document.storage_path)
    stored_path.write_bytes(b"altered bytes")
    assert stored_path.stat().st_size == len(b"trusted bytes")
    prepare_readiness_database(monkeypatch, tmp_path)

    response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["storage"] == {
        "status": "not_ready",
        "reason": "storage_inconsistent",
    }


def test_hanging_identity_probe_holds_no_database_session(monkeypatch, tmp_path):
    prepare_readiness_database(monkeypatch, tmp_path)
    monkeypatch.setattr("app.main.settings.embedding_provider", "ollama")
    entered_provider = threading.Event()
    release_provider = threading.Event()
    session_lock = threading.Lock()
    active_sessions = 0
    real_session_factory = SessionLocal

    @contextmanager
    def tracked_session():
        nonlocal active_sessions
        session = real_session_factory()
        with session_lock:
            active_sessions += 1
        try:
            yield session
        finally:
            session.close()
            with session_lock:
                active_sessions -= 1

    class HangingProvider:
        def resolve_identity_for_readiness(self):
            entered_provider.set()
            assert release_provider.wait(timeout=2)
            return SimpleNamespace(
                value="ollama:test@sha256:abc:dim768:embeddinggemma-retrieval-v1"
            )

    monkeypatch.setattr("app.main.SessionLocal", tracked_session)
    monkeypatch.setattr("app.main.get_embedding_provider", lambda: HangingProvider())
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(__import__("app.main", fromlist=["_readiness_report"])._readiness_report)
        assert entered_provider.wait(timeout=2)
        with session_lock:
            assert active_sessions == 0
        release_provider.set()
        status_code, report = result.result(timeout=2)

    assert status_code == 200
    assert report["checks"]["retrieval"] == {
        "status": "ready",
        "backend": "native-pgvector",
        "indexed": True,
    }


def test_storage_readiness_is_single_flight_and_cached(monkeypatch, tmp_path):
    main_module._reset_storage_readiness_cache()
    monkeypatch.setattr("app.main.settings.upload_dir", str(tmp_path))
    monkeypatch.setattr("app.main.settings.storage_readiness_wait_seconds", 1)
    calls = 0
    entered = threading.Event()
    release = threading.Event()

    def slow_check(_documents):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return {"status": "ready"}

    monkeypatch.setattr("app.main._storage_check", slow_check)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(main_module._cached_storage_check, []) for _index in range(6)]
        assert entered.wait(timeout=2)
        release.set()
        assert [future.result(timeout=2) for future in futures] == [{"status": "ready"}] * 6
    assert calls == 1

    assert main_module._cached_storage_check([]) == {"status": "ready"}
    assert calls == 1


def test_storage_readiness_failure_cache_expires_and_recovers(monkeypatch, tmp_path):
    main_module._reset_storage_readiness_cache()
    monkeypatch.setattr("app.main.settings.upload_dir", str(tmp_path))
    monkeypatch.setattr("app.main.settings.storage_readiness_failure_ttl_seconds", 5)
    clock = [100.0]
    calls = 0

    def monotonic():
        return clock[0]

    def flaky_check(_documents):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("disk temporarily unavailable")
        return {"status": "ready"}

    monkeypatch.setattr("app.main.time.monotonic", monotonic)
    monkeypatch.setattr("app.main._storage_check", flaky_check)
    with pytest.raises(RuntimeError):
        main_module._cached_storage_check([])
    with pytest.raises(RuntimeError):
        main_module._cached_storage_check([])
    assert calls == 1

    clock[0] += 6
    assert main_module._cached_storage_check([]) == {"status": "ready"}
    assert calls == 2


def test_page_search_and_delete_flow():
    created = client.post(
        "/api/v1/pages",
        json={"title": "Storage decision", "content": "Atlas stores files locally by default."},
    )
    assert created.status_code == 201
    page = created.json()
    assert page["slug"] == "storage-decision"

    results = client.get("/api/v1/search", params={"q": "locally"}).json()
    assert results[0]["page_id"] == page["id"]

    answer = client.post("/api/v1/ask", json={"question": "locally"}).json()
    assert answer["evidence"] == "provider_required"
    assert len(answer["citations"]) == 1

    assert client.delete(f"/api/v1/pages/{page['id']}").status_code == 204
    assert client.get("/api/v1/search", params={"q": "locally"}).json() == []


def test_pages_can_be_categorized_created_filtered_and_updated():
    concept = client.post("/api/v1/pages", json={"title": "RAG", "content": "x", "category": "concepts"}).json()
    uncategorized = client.post("/api/v1/pages", json={"title": "Scratch"}).json()
    assert concept["category"] == "concepts"
    assert uncategorized["category"] is None

    assert client.get("/api/v1/categories").json() == ["concepts"]

    filtered = client.get("/api/v1/pages", params={"category": "concepts"}).json()
    assert [page["id"] for page in filtered] == [concept["id"]]

    recategorized = client.patch(f"/api/v1/pages/{uncategorized['id']}", json={"category": "concepts"}).json()
    assert recategorized["category"] == "concepts"
    assert recategorized["title"] == "Scratch"  # category-only PATCH leaves content untouched

    cleared = client.patch(f"/api/v1/pages/{concept['id']}", json={"category": ""}).json()
    assert cleared["category"] is None


def test_category_can_be_set_on_an_otherwise_immutable_imported_page():
    imported = client.post(
        "/api/v1/imports",
        files={"file": ("source.txt", b"Immutable source", "text/plain")},
    ).json()["page"]

    categorized = client.patch(f"/api/v1/pages/{imported['id']}", json={"category": "imports"})
    assert categorized.status_code == 200
    assert categorized.json()["category"] == "imports"

    still_immutable = client.patch(f"/api/v1/pages/{imported['id']}", json={"content": "changed"})
    assert still_immutable.status_code == 409


def test_duplicate_titles_receive_unique_slugs():
    first = client.post("/api/v1/pages", json={"title": "Hello"}).json()
    second = client.post("/api/v1/pages", json={"title": "Hello"}).json()
    assert first["slug"] == "hello"
    assert second["slug"] == "hello-2"


def test_text_import_is_chunked_and_deduplicated():
    payload = b"# Atlas\n\nAtlas keeps its source trail.\n\n## Storage\n\nFiles stay local."
    first = client.post("/api/v1/imports", files={"file": ("atlas.md", payload, "text/markdown")})
    assert first.status_code == 201
    body = first.json()
    assert body["duplicate"] is False
    assert body["chunks_created"] == 2

    second = client.post("/api/v1/imports", files={"file": ("copy.md", payload, "text/markdown")})
    assert second.status_code == 201
    assert second.json()["duplicate"] is True

    semantic = client.get("/api/v1/search", params={"q": "storage", "mode": "semantic"})
    assert semantic.status_code == 200
    assert semantic.json()


def test_import_rejects_empty_and_unsupported_files():
    empty = client.post("/api/v1/imports", files={"file": ("empty.txt", b"", "text/plain")})
    assert empty.status_code == 422
    unsupported = client.post("/api/v1/imports", files={"file": ("image.png", b"png", "image/png")})
    assert unsupported.status_code == 415


def test_ask_reports_insufficient_evidence():
    answer = client.post("/api/v1/ask", json={"question": "unknown subject"})
    assert answer.status_code == 200
    assert answer.json() == {
        "answer": "Atlas Wiki could not find evidence for this question in your knowledge base.",
        "evidence": "insufficient",
        "citations": [],
    }


def test_ask_returns_sanitized_provider_unavailable_error(monkeypatch):
    from app.providers import ModelUnavailableError

    created = client.post(
        "/api/v1/pages",
        json={"title": "Provider failure", "content": "Traceable provider evidence."},
    )
    assert created.status_code == 201

    async def unavailable(_question, _evidence):
        raise ModelUnavailableError("Model provider is unavailable")

    monkeypatch.setattr("app.main.generate_answer", unavailable)
    answer = client.post("/api/v1/ask", json={"question": "provider evidence"})

    assert answer.status_code == 503
    assert answer.json() == {
        "detail": {
            "code": "provider_unavailable",
            "message": "Model provider is unavailable",
            "retryable": True,
        }
    }


def test_generated_answer_gets_a_citation_marker(monkeypatch):
    long_evidence = "Citations make claims traceable. " + "context " * 80
    client.post("/api/v1/pages", json={"title": "Grounding", "content": long_evidence})

    async def fake_generate(_question, evidence):
        assert len(evidence[0]) > 400
        return "Claims are traceable [1]."

    monkeypatch.setattr("app.main.generate_answer", fake_generate)
    answer = client.post("/api/v1/ask", json={"question": "traceable"})
    assert answer.status_code == 200
    assert answer.json()["evidence"] == "sufficient"
    assert "[1]" in answer.json()["answer"]


def test_generated_answer_without_valid_citation_is_rejected(monkeypatch):
    client.post("/api/v1/pages", json={"title": "Evidence", "content": "A grounded fact."})

    async def fake_generate(_question, _evidence):
        return "An unsupported claim."

    monkeypatch.setattr("app.main.generate_answer", fake_generate)
    answer = client.post("/api/v1/ask", json={"question": "grounded fact"}).json()
    assert answer["evidence"] == "insufficient"
    assert "did not produce verifiable" in answer["answer"]


def test_citation_validation_allows_dotted_identifiers(monkeypatch):
    client.post("/api/v1/pages", json={"title": "Instructions", "content": "AGENTS.md contains project guidance."})

    async def fake_generate(_question, _evidence):
        return "Codex reads `AGENTS.md` for project guidance [1]."

    monkeypatch.setattr("app.main.generate_answer", fake_generate)
    answer = client.post("/api/v1/ask", json={"question": "AGENTS.md guidance"}).json()
    assert answer["evidence"] == "sufficient"


def test_uncited_claim_without_space_after_terminator_is_rejected(monkeypatch):
    client.post("/api/v1/pages", json={"title": "Facts", "content": "A supported fact."})

    async def fake_generate(_question, _evidence):
        return "Supported fact [1].Fabricated claim!Encryption fails。伪造事实"

    monkeypatch.setattr("app.main.generate_answer", fake_generate)
    answer = client.post("/api/v1/ask", json={"question": "supported fact"}).json()
    assert answer["evidence"] == "insufficient"
    assert "did not produce verifiable" in answer["answer"]


def test_ask_exposes_the_same_complete_chunk_sent_to_the_model(monkeypatch):
    long_evidence = "traceable start " + "context " * 100 + "traceable end"
    client.post("/api/v1/pages", json={"title": "Full evidence", "content": long_evidence})

    async def fake_generate(_question, evidence):
        assert evidence[0] == long_evidence
        return "The evidence has a traceable end [1]."

    monkeypatch.setattr("app.main.generate_answer", fake_generate)
    answer = client.post("/api/v1/ask", json={"question": "traceable end"}).json()
    assert answer["evidence"] == "sufficient"
    assert answer["citations"][0]["excerpt"] == long_evidence


def test_cited_model_refusal_is_reported_as_insufficient(monkeypatch):
    client.post(
        "/api/v1/pages",
        json={"title": "Scope", "content": "Questions about baking bread are outside this software documentation's scope."},
    )

    async def fake_generate(_question, _evidence):
        return "INSUFFICIENT_EVIDENCE The sources contain no baking instructions [1]."

    monkeypatch.setattr("app.main.generate_answer", fake_generate)
    answer = client.post("/api/v1/ask", json={"question": "How do I bake bread?"}).json()
    assert answer["evidence"] == "insufficient"
    assert answer["answer"] == "The sources contain no baking instructions [1]."


def test_search_handles_one_thousand_pages():
    from app.database import SessionLocal
    from app.main import rebuild_chunks
    from app.models import Page

    with SessionLocal() as db:
        pages = [Page(title=f"Note {index}", slug=f"note-{index}", content=f"Topic {index} searchable knowledge") for index in range(1000)]
        for page in pages:
            rebuild_chunks(page)
        db.add_all(pages)
        db.commit()

    response = client.get("/api/v1/search", params={"q": "Topic 777", "mode": "hybrid", "limit": 5})
    assert response.status_code == 200
    assert response.json()[0]["title"] == "Note 777"


def test_imported_page_is_immutable_and_wiki_links_are_reported():
    imported = client.post(
        "/api/v1/imports",
        files={"file": ("source.txt", b"Immutable source", "text/plain")},
    ).json()["page"]
    update = client.patch(f"/api/v1/pages/{imported['id']}", json={"content": "changed"})
    assert update.status_code == 409

    target = client.post("/api/v1/pages", json={"title": "Target", "content": "destination"}).json()
    source = client.post("/api/v1/pages", json={"title": "Source", "content": "See [[target]]."}).json()
    links = client.get(f"/api/v1/pages/{source['id']}/links").json()
    assert [page["id"] for page in links["outbound"]] == [target["id"]]
    backlinks = client.get(f"/api/v1/pages/{target['id']}/links").json()
    assert [page["id"] for page in backlinks["backlinks"]] == [source["id"]]


def test_cross_site_writes_are_rejected():
    response = client.post(
        "/api/v1/pages",
        headers={"origin": "https://attacker.example"},
        json={"title": "Cross-site"},
    )
    assert response.status_code == 403


def test_page_slug_remains_stable_after_rename():
    page = client.post("/api/v1/pages", json={"title": "Stable Link"}).json()
    renamed = client.patch(f"/api/v1/pages/{page['id']}", json={"title": "New Display Name"}).json()
    assert renamed["title"] == "New Display Name"
    assert renamed["slug"] == "stable-link"


def test_page_rename_invalidates_existing_native_embeddings():
    page = client.post(
        "/api/v1/pages", json={"title": "Old Title", "content": "content to embed"}
    ).json()
    with SessionLocal() as db:
        chunk = db.scalar(select(Chunk).where(Chunk.page_id == page["id"]))
        chunk.embedding = [1.0] + [0.0] * 767
        chunk.embedding_model = "test-model"
        chunk.embedding_version = "test-version"
        from datetime import datetime, timezone
        chunk.embedding_updated_at = datetime.now(timezone.utc)
        db.commit()

    assert client.patch(f"/api/v1/pages/{page['id']}", json={"title": "New Title"}).status_code == 200
    with SessionLocal() as db:
        chunks = db.scalars(select(Chunk).where(Chunk.page_id == page["id"])).all()
        assert chunks
        assert all(chunk.embedding is None and chunk.embedding_model is None for chunk in chunks)


def test_document_response_hides_internal_storage_path():
    client.post("/api/v1/imports", files={"file": ("private.txt", b"private source", "text/plain")})
    document = client.get("/api/v1/documents").json()[0]
    assert "storage_path" not in document


def test_repeated_delete_returns_not_found():
    page = client.post("/api/v1/pages", json={"title": "Delete once"}).json()
    assert client.delete(f"/api/v1/pages/{page['id']}").status_code == 204
    assert client.delete(f"/api/v1/pages/{page['id']}").status_code == 404
