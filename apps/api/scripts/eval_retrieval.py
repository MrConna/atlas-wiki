#!/usr/bin/env python3
import argparse
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


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
    args = parser.parse_args()

    fixture_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "retrieval"
    manifest = json.loads((fixture_dir / "manifest.json").read_text())
    queries = load_jsonl(fixture_dir / "queries.jsonl")

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
        "backend": "native-pgvector" if args.base_url else "feature-hash",
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
        gates = {
            "recall_at_5": report["metrics"]["recall_at_5"] >= 0.80,
            "mrr_at_10": report["metrics"]["mrr_at_10"] >= 0.65,
            "cross_doc_all_relevant_at_5": report["metrics"]["cross_doc_all_relevant_at_5"] >= 0.75,
            "unrelated_false_positive_rate": report["metrics"]["unrelated_false_positive_rate"] <= 0.10,
            "paraphrase_recall_at_5": report["class_recall_at_5"].get("paraphrase", 0) >= 0.80,
            "adversarial_recall_at_5": report["class_recall_at_5"].get("adversarial", 0) >= 0.90,
        }
        failed = [name for name, passed in gates.items() if not passed]
        report["release_gates"] = gates
        if failed:
            raise SystemExit(f"Native retrieval gates failed: {', '.join(failed)}")
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.json_output:
        args.json_output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
