# Atlas Wiki

A local-first personal LLM wiki with traceable citations.

## Current foundation

- FastAPI service with health, page, search, and ask endpoints
- PostgreSQL + native pgvector multilingual retrieval
- Next.js dashboard
- Docker Compose local environment
- Product requirements and API contract
- Architecture and security notes in `docs/`

## Run locally

```bash
cp .env.example .env
docker compose up --build
```

Open the web app at http://localhost:3000 and API docs at http://localhost:8000/docs.
`/api/v1/health` is process liveness; `/api/v1/ready` verifies the database,
migrations, storage, and configured embedding model. Compose waits for
readiness before starting the web service.

For non-default loopback ports, update `API_PORT`, `WEB_PORT`,
`NEXT_PUBLIC_API_URL`, and `CORS_ORIGINS` in `.env`, then rebuild the web image.
`NEXT_PUBLIC_API_URL` is compiled into the browser bundle and is not a runtime
setting. Atlas has no authentication and must not be exposed to an untrusted
LAN or the public internet.

## Model providers

Atlas works without a model provider for import and retrieval. To generate grounded answers, set one of:

```dotenv
MODEL_PROVIDER=ollama
MODEL_NAME=qwen3:8b
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

or an OpenAI-compatible API:

```dotenv
MODEL_PROVIDER=openai
MODEL_NAME=your-model-id
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://api.openai.com/v1
```

DeepSeek is available as a first-class OpenAI-compatible provider:

```dotenv
MODEL_PROVIDER=deepseek
MODEL_NAME=deepseek-v4-flash
DEEPSEEK_API_KEY=your-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
MODEL_CONNECT_TIMEOUT_SECONDS=5
MODEL_READ_TIMEOUT_SECONDS=120
MODEL_TOTAL_TIMEOUT_SECONDS=150
MODEL_MAX_RETRIES=2
MODEL_MAX_CONCURRENCY=4
MODEL_MAX_TOKENS=2048
```

Transient connection, timeout, rate-limit, and server failures are retried with
bounded backoff. Atlas returns a sanitized `503` when the provider remains
unavailable and a sanitized `502` for malformed responses; prompts, evidence,
API keys, and provider response bodies are never included in API errors.

An opt-in smoke test makes one small billable request and prints no prompt,
evidence, answer, headers, or credential:

```bash
RUN_DEEPSEEK_SMOKE=1 docker compose exec -e RUN_DEEPSEEK_SMOKE=1 api \
  python scripts/smoke_deepseek.py
```

Retrieved evidence is treated as untrusted text. The model must cite evidence with valid `[n]` markers; Atlas rejects generated answers that omit or invent citation identifiers.

The default `legacy` embedding mode remains deterministic and credential-free for rollback. The recommended production mode below uses local multilingual embeddings and calibrated no-answer thresholds.

## Native local embeddings

Atlas supports a private multilingual embedding path using Ollama, EmbeddingGemma, and PostgreSQL pgvector. DeepSeek remains the answer-generation model; document and query embeddings stay on the local Ollama service.

Start the optional bundled Ollama service and pull the pinned model tag:

```bash
export APP_GIT_SHA="$(git rev-parse HEAD)"
docker compose -f compose.yaml -f compose.ollama.yaml up -d --build
docker compose exec api python -m app.cli embeddings-backfill
```

Backfill is resumable and model-aware. It preserves the old feature-hash vectors for rollback and commits each validated batch atomically. The optional model volume is persistent and Ollama is not exposed on a host port.

## Backup and restore

Back up PostgreSQL and uploaded source files from one stopped-write window:

```bash
scripts/backup.sh /secure/path/atlas-backup.tar.gz
```

The generated archive contains document content and is **not encrypted by
Atlas**. Store it only on encrypted storage or encrypt it before copying it.

Restoration verifies checksums and refuses to overwrite non-empty data. See
[`docs/BACKUP_RESTORE.md`](docs/BACKUP_RESTORE.md) for clean-environment
recovery, integrity checks, encryption guidance, and retention policy.

## Development

API:

```bash
cd apps/api
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
alembic upgrade head
pytest
uvicorn app.main:app --reload
```

Web:

```bash
cd apps/web
npm install
npm run dev
```
