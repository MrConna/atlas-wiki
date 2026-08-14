import json
from pathlib import Path


def test_retrieval_fixture_is_well_formed():
    fixture_dir = Path(__file__).parent / "fixtures" / "retrieval"
    manifest = json.loads((fixture_dir / "manifest.json").read_text())
    doc_keys = {document["doc_key"] for document in manifest["documents"]}
    assert doc_keys == {"codex-prompting", "codex-agents-md"}
    assert all((fixture_dir / document["file"]).is_file() for document in manifest["documents"])

    queries = [json.loads(line) for line in (fixture_dir / "queries.jsonl").read_text().splitlines()]
    assert len(queries) >= 20
    assert {query["class"] for query in queries} == {"exact", "paraphrase", "cross_doc", "unrelated", "adversarial"}
    assert len({query["id"] for query in queries}) == len(queries)
    assert all(set(query["relevant"]) <= doc_keys for query in queries)
    assert all(bool(query["relevant"]) == query["expect_answerable"] for query in queries)
