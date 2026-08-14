import json
import subprocess
import sys
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


def test_hybrid_retrieval_baseline_is_executable_and_stable():
    api_dir = Path(__file__).parents[1]
    command = [sys.executable, "scripts/eval_retrieval.py", "--mode", "hybrid"]
    first = subprocess.run(command, cwd=api_dir, check=True, capture_output=True, text=True).stdout
    second = subprocess.run(command, cwd=api_dir, check=True, capture_output=True, text=True).stdout
    assert first == second

    report = json.loads(first)
    assert report["query_count"] == 20
    assert report["metrics"]["hit_rate_at_5"] == 0.75
    assert report["metrics"]["cross_doc_all_relevant_at_5"] == 0.5
    assert report["metrics"]["unrelated_false_positive_rate"] == 0.5
    assert report["class_hit_rate_at_5"]["exact"] == 1.0
