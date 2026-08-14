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
```

Retrieved evidence is treated as untrusted text. The model must cite evidence with valid `[n]` markers; Atlas rejects generated answers that omit or invent citation identifiers.

The default `legacy` embedding mode remains deterministic and credential-free for rollback. The recommended production mode below uses local multilingual embeddings and calibrated no-answer thresholds.

## Native local embeddings

Atlas supports a private multilingual embedding path using Ollama, EmbeddingGemma, and PostgreSQL pgvector. DeepSeek remains the answer-generation model; document and query embeddings stay on the local Ollama service.

Start the optional bundled Ollama service and pull the pinned model tag:

```bash
docker compose -f compose.yaml -f compose.ollama.yaml up -d --build
docker compose exec api python -m app.cli embeddings-backfill
```

Backfill is resumable and model-aware. It preserves the old feature-hash vectors for rollback and commits each validated batch atomically. The optional model volume is persistent and Ollama is not exposed on a host port.

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
