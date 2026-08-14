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

## Feature-hash baseline

Measured on fixture version 1 before the pgvector migration:

| Mode | Hit@1 | Hit@5 | MRR@10 | Cross-doc all@5 | Unrelated FP |
| --- | ---: | ---: | ---: | ---: | ---: |
| keyword | 0.625 | 0.625 | 0.625 | 0.50 | 0.75 |
| semantic | 0.688 | 0.750 | 0.708 | 1.00 | 1.00 |
| hybrid | 0.750 | 0.750 | 0.750 | 0.50 | 0.75 |

Hybrid exact Recall@5 is 1.00, while bilingual paraphrase Recall@5 is 0.667 and adversarial Recall@5 is 0.50. Most importantly, three of four unrelated queries return false-positive results in hybrid mode, and all four do in semantic mode. These measurements are descriptive baselines, not passing release thresholds; they make the expected benefit of real multilingual embeddings and calibrated rejection measurable.
