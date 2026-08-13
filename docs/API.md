# API contract

Base path: `/api/v1`

## Health

`GET /health` returns service status.

## Pages

- `GET /pages` lists wiki pages.
- `POST /pages` creates a page.
- `GET /pages/{page_id}` returns a page.
- `PATCH /pages/{page_id}` updates a page.
- `DELETE /pages/{page_id}` deletes a page.

## Search

`GET /search?q=...&mode=hybrid&limit=10`

Modes are `keyword`, `semantic`, and `hybrid`. Results contain page and chunk identifiers, a text excerpt, score, and source location.

## Ask

`POST /ask`

Request:

```json
{"question":"What did I decide about storage?","limit":8}
```

Response includes an answer, evidence status, and citations. Until a model provider is configured, the endpoint returns retrieved evidence with a setup message.
