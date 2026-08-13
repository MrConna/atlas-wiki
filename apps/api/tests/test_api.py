from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app


client = TestClient(app)


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_health():
    assert client.get("/api/v1/health").json() == {"status": "ok"}


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


def test_document_response_hides_internal_storage_path():
    client.post("/api/v1/imports", files={"file": ("private.txt", b"private source", "text/plain")})
    document = client.get("/api/v1/documents").json()[0]
    assert "storage_path" not in document


def test_repeated_delete_returns_not_found():
    page = client.post("/api/v1/pages", json={"title": "Delete once"}).json()
    assert client.delete(f"/api/v1/pages/{page['id']}").status_code == 204
    assert client.delete(f"/api/v1/pages/{page['id']}").status_code == 404
