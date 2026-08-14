#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def native_environment_gates(metadata: dict, git_sha: str, model_digest: str, expected_corpus: list[dict]) -> dict:
    identity = str(metadata.get("model_identity", ""))
    return {
        "native_backend": metadata.get("backend") == "native-pgvector",
        "deployed_git_sha": metadata.get("app_git_sha") == git_sha,
        "embedding_dimensions": metadata.get("dimensions") == 768,
        "embedding_model_digest": f"@{model_digest}:dim768:" in identity,
        "all_chunks_current": metadata.get("total_chunks") == metadata.get("current_chunks"),
        "exact_fixture_corpus": metadata.get("corpus") == expected_corpus,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Atlas retrieval through its public API")
    parser.add_argument("--mode", choices=("keyword", "semantic", "hybrid"), default="hybrid")
    parser.add_argument("--json", dest="json_output", type=Path)
    parser.add_argument(
        "--base-url",
        help="Evaluate an already populated API (for native pgvector/Ollama integration runs)",
    )
    parser.add_argument(
        "--assert-native-gates",
        action="store_true",
        help="Fail unless the native hybrid retrieval release gates are met",
    )
    parser.add_argument("--git-sha", help="Exact 40-character commit SHA recorded in native reports")
    parser.add_argument(
        "--expected-model-digest",
        help="Exact Ollama model digest required by the native release gate",
    )
    args = parser.parse_args()

    fixture_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "retrieval"
    manifest = json.loads((fixture_dir / "manifest.json").read_text())
    queries = load_jsonl(fixture_dir / "queries.jsonl")
    fixture_hashes = {
        str(path.relative_to(fixture_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(fixture_dir.rglob("*"))
        if path.is_file()
    }
    expected_corpus = []
    for document in manifest["documents"]:
        path = fixture_dir / document["file"]
        content = path.read_text(encoding="utf-8-sig").strip()
        title = Path(document["file"]).stem
        from app.retrieval import chunk_text

        expected_corpus.append(
            {
                "title": title,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "chunk_count": len(chunk_text(content, title)),
                "current_chunk_count": len(chunk_text(content, title)),
            }
        )
    expected_corpus.sort(key=lambda item: (item["title"], item["content_sha256"]))

    def execute_queries(client) -> list[dict]:
        rows = []
        for query in queries:
            response = client.get(
                "/api/v1/search",
                params={"q": query["query"], "mode": args.mode, "limit": 10},
            )
            response.raise_for_status()
            results = response.json()
            repeat = client.get(
                "/api/v1/search",
                params={"q": query["query"], "mode": args.mode, "limit": 10},
            ).json()
            stable_fields = lambda items: [
                (item["title"], item["heading_path"], item["excerpt"], item["score"]) for item in items
            ]
            if stable_fields(results) != stable_fields(repeat):
                raise RuntimeError(f"Non-deterministic ranking for {query['id']}")
            titles = [item["title"] for item in results]
            relevant = query["relevant"]
            ranks = [titles.index(title) + 1 for title in relevant if title in titles]
            rows.append(
                {
                    **query,
                    "returned": titles,
                    "ranks": ranks,
                    "max_score": max((item["score"] for item in results), default=0.0),
                }
            )
        return rows

    if args.base_url:
        import httpx

        with httpx.Client(base_url=args.base_url, timeout=120) as client:
            retrieval_status = client.get("/api/v1/retrieval/status")
            retrieval_status.raise_for_status()
            backend_metadata = retrieval_status.json()
            rows = execute_queries(client)
    else:
        with tempfile.TemporaryDirectory(prefix="atlas-retrieval-eval-") as temp_dir:
            os.environ["DATABASE_URL"] = f"sqlite:///{Path(temp_dir) / 'eval.db'}"
            os.environ["UPLOAD_DIR"] = str(Path(temp_dir) / "uploads")
            os.environ["MODEL_PROVIDER"] = "none"

            from fastapi.testclient import TestClient
            from app.database import Base, engine
            from app.main import app

            Base.metadata.create_all(engine)

            with TestClient(app) as client:
                for document in manifest["documents"]:
                    path = fixture_dir / document["file"]
                    response = client.post(
                        "/api/v1/imports",
                        files={"file": (path.name, path.read_bytes(), "text/markdown")},
                    )
                    response.raise_for_status()
                rows = execute_queries(client)
            backend_metadata = {"backend": "feature-hash"}

    answerable = [row for row in rows if row["expect_answerable"]]
    unrelated = [row for row in rows if not row["expect_answerable"]]
    by_class = defaultdict(list)
    for row in rows:
        by_class[row["class"]].append(row)

    def hit_rate_at(group: list[dict], k: int) -> float:
        return sum(bool(row["ranks"] and min(row["ranks"]) <= k) for row in group) / len(group) if group else 0.0

    def recall_at(group: list[dict], k: int) -> float:
        return (
            sum(sum(rank <= k for rank in row["ranks"]) / len(row["relevant"]) for row in group) / len(group)
            if group
            else 0.0
        )

    report = {
        "backend": backend_metadata["backend"],
        "backend_metadata": backend_metadata,
        "git_sha": args.git_sha,
        "fixture_sha256": fixture_hashes,
        "mode": args.mode,
        "query_count": len(rows),
        "metrics": {
            "hit_rate_at_1": hit_rate_at(answerable, 1),
            "hit_rate_at_3": hit_rate_at(answerable, 3),
            "hit_rate_at_5": hit_rate_at(answerable, 5),
            "recall_at_1": recall_at(answerable, 1),
            "recall_at_3": recall_at(answerable, 3),
            "recall_at_5": recall_at(answerable, 5),
            "mrr_at_10": sum((1 / min(row["ranks"]) if row["ranks"] else 0) for row in answerable) / len(answerable),
            "cross_doc_all_relevant_at_5": sum(
                len(row["ranks"]) == len(row["relevant"]) and max(row["ranks"], default=99) <= 5
                for row in by_class["cross_doc"]
            )
            / len(by_class["cross_doc"]),
            "unrelated_false_positive_rate": sum(bool(row["returned"]) for row in unrelated) / len(unrelated),
        },
        "class_hit_rate_at_5": {name: hit_rate_at(group, 5) for name, group in sorted(by_class.items())},
        "class_recall_at_5": {
            name: recall_at([row for row in group if row["expect_answerable"]], 5)
            for name, group in sorted(by_class.items())
            if any(row["expect_answerable"] for row in group)
        },
        "queries": rows,
    }
    if args.assert_native_gates:
        if not args.base_url or args.mode != "hybrid":
            parser.error("--assert-native-gates requires --base-url and --mode hybrid")
        if not args.git_sha or not re.fullmatch(r"[0-9a-f]{40}", args.git_sha):
            parser.error("--assert-native-gates requires --git-sha with the exact 40-character commit SHA")
        if not args.expected_model_digest or not re.fullmatch(r"[0-9a-f]{64}", args.expected_model_digest):
            parser.error("--assert-native-gates requires --expected-model-digest with 64 lowercase hex characters")
        gates = {
            **native_environment_gates(
                backend_metadata, args.git_sha, args.expected_model_digest, expected_corpus
            ),
            "recall_at_5": report["metrics"]["recall_at_5"] >= 0.80,
            "mrr_at_10": report["metrics"]["mrr_at_10"] >= 0.65,
            "cross_doc_all_relevant_at_5": report["metrics"]["cross_doc_all_relevant_at_5"] >= 0.75,
            "unrelated_false_positive_rate": report["metrics"]["unrelated_false_positive_rate"] <= 0.10,
            "exact_recall_at_3": recall_at(by_class["exact"], 3) >= 0.98,
            "paraphrase_recall_at_5": report["class_recall_at_5"].get("paraphrase", 0) >= 0.80,
            "paraphrase_improvement_over_legacy": report["class_recall_at_5"].get("paraphrase", 0)
            >= (2 / 3 + 0.20),
            "adversarial_recall_at_5": report["class_recall_at_5"].get("adversarial", 0) >= 0.90,
        }
        failed = [name for name, passed in gates.items() if not passed]
        report["release_gates"] = gates
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.json_output:
        args.json_output.write_text(rendered + "\n")
    print(rendered)
    if args.assert_native_gates and failed:
        raise SystemExit(f"Native retrieval gates failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
