# Atlas Wiki MVP PRD

## Goal

Create a single-user, local-first knowledge base that imports personal documents, supports wiki navigation and hybrid retrieval, and answers questions only with traceable citations.

## In scope

- Markdown, plain text, and PDF import
- Structural chunking with source metadata
- Full-text and vector retrieval
- Citation-grounded answers
- Wiki page editing, outbound links, and backlinks

Wiki links use permanent page slugs: `[[target-page-slug]]`. Renaming a page does not change its slug.
- Local file storage and Docker Compose startup
- OpenAI-compatible and Ollama model adapters

## Out of scope

- Multi-user access control
- Real-time collaboration
- Mobile applications
- Internet-wide crawling
- Model fine-tuning
- Knowledge graph visualization

## Core acceptance criteria

1. Import Markdown, TXT, and text-based PDF files.
2. Re-importing identical content does not create duplicates.
3. Search supports keyword, semantic, and hybrid modes.
4. Every factual answer exposes at least one clickable source citation.
5. The assistant reports insufficient evidence instead of inventing an answer.
6. Deleting a document removes its chunks from retrieval.
7. Docker Compose starts the application from a clean checkout.
