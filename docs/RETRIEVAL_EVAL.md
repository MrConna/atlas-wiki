# Retrieval evaluation

Atlas uses a fixed, offline bilingual golden set to compare retrieval backends without involving an LLM. The corpus contains original summaries of two linked official OpenAI Codex documents; it does not redistribute the source pages verbatim.

Run the current baseline from `apps/api`:

```bash
python scripts/eval_retrieval.py --mode hybrid --json retrieval-baseline.json
```

The query set covers exact terms, English and Chinese paraphrases, cross-document questions, unrelated questions, and adversarial wording. Reports include Hit Rate@1/3/5, standard Recall@1/3/5, MRR@10, cross-document all-relevant Recall@5, unrelated false-positive rate, per-class metrics, deterministic rerun checks, and per-query ranks/scores. Hit Rate asks whether at least one relevant source was retrieved; Recall measures the fraction of all relevant sources retrieved.

## Release targets for native vector retrieval

- exact Recall@3 >= 0.95
- paraphrase Recall@5 >= 0.80 and MRR@10 >= 0.65
- cross-document all-relevant Recall@5 >= 0.75
- unrelated false-positive rate <= 0.10
- adversarial Recall@5 >= 0.90 with correct source identity
- paraphrase Recall@5 improves by at least 0.20 over the feature-hash baseline
- exact retrieval does not regress by more than 0.02

The fixture is intentionally small and is a first release gate, not a claim of general retrieval quality. Model name, revision, dimensions, distance metric, index type, git SHA, and fixture version must accompany future pgvector reports.

Run the native integration gate against an already populated Atlas API containing the fixture documents:

```bash
python scripts/eval_retrieval.py --mode hybrid \
  --base-url http://127.0.0.1:8000 \
  --git-sha "$(git rev-parse HEAD)" \
  --expected-model-digest 101341d65c2ccbf23f16650b79d30b9fca94a45ffa09a9984c600157b81a58df \
  --assert-native-gates --json retrieval-native.json
```

## Native pgvector result

Measured in a clean database containing exactly the two fixture documents, with Ollama 0.11.10, `embeddinggemma:300m-qat-q4_0` digest `101341d65c2ccbf23f16650b79d30b9fca94a45ffa09a9984c600157b81a58df`, 768 dimensions, exact pgvector cosine search, and fixture version 1:

| Mode | Hit@1 | Hit@5 | Recall@5 | MRR@10 | Cross-doc all@5 | Unrelated FP |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| hybrid | 1.000 | 1.000 | 1.000 | 1.000 | 1.00 | 0.00 |

Exact, paraphrase, cross-document, and adversarial class Recall@5 are all 1.00. Query decomposition is deterministic, capped at four parts, and strips leading control-language clauses before embedding; adding control language never lowers the confidence threshold. Keyword overlap is a bounded reranking signal and only rescues strong exact-term matches. These rules preserve rejection for all unrelated fixture queries while recovering multi-intent and prompt-injection-shaped questions. The gate verifies the server-reported native backend/model identity, exact fixture-only page set, fixture hashes, and exact Git SHA; it writes its JSON report even when a metric fails.

## Feature-hash baseline

Measured on fixture version 1 before the pgvector migration:

| Mode | Hit@1 | Hit@5 | MRR@10 | Cross-doc all@5 | Unrelated FP |
| --- | ---: | ---: | ---: | ---: | ---: |
| keyword | 0.563 | 0.563 | 0.563 | 0.50 | 0.25 |
| semantic | 0.688 | 0.750 | 0.708 | 1.00 | 1.00 |
| hybrid | 0.625 | 0.750 | 0.688 | 1.00 | 0.50 |

Hybrid exact Recall@5 is 1.00, while bilingual paraphrase Recall@5 is 0.667 and adversarial Recall@5 is 0.50. Even after stop-word filtering, two of four unrelated queries return false-positive results in hybrid mode, and all four do in legacy semantic mode. These measurements are descriptive baselines, not passing release thresholds; they make the benefit of real multilingual embeddings and calibrated rejection measurable.

Hybrid Hit@1/MRR@10 and cross-document Recall@5 were recalibrated after fixing a chunker bug where a heading line glued to its body paragraph (no blank line between them) leaked the entire following paragraph into `heading`/`source_location`, which both truncated on long values and duplicated the heading text into chunk content. The corrected chunk boundaries shift some rank-1 hits but resolve the multi-document cross-doc queries that previously missed one of two relevant sources.
