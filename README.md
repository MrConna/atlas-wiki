# Atlas Wiki

A local-first personal LLM wiki with traceable citations.

## Current foundation

- FastAPI service with health, page, search, and ask endpoints
- PostgreSQL + pgvector-ready schema
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

Retrieved evidence is treated as untrusted text. The model must cite evidence with valid `[n]` markers; Atlas rejects generated answers that omit or invent citation identifiers.

The default local embedding is deterministic and credential-free. It is designed for the MVP scale; see `docs/ARCHITECTURE.md` for the native pgvector migration boundary.

## Development

API:

```bash
cd apps/api
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
uvicorn app.main:app --reload
```

Web:

```bash
cd apps/web
npm install
npm run dev
```
