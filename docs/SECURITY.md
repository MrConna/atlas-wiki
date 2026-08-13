# Security notes

## Implemented controls

- Imports are limited to Markdown, UTF-8 text, and PDF.
- Upload size defaults to 10 MiB and can be configured.
- Total local document storage defaults to 1 GiB; cross-site write origins are rejected.
- Client filenames are reduced to a basename; stored filenames are content hashes.
- Duplicate content is not stored twice.
- Retrieved documents are explicitly treated as untrusted data in the model prompt.
- Model failures return a generic error class instead of provider response bodies or credentials.
- CORS defaults to the local web origin.
- Trusted hosts are configurable through `ALLOWED_HOSTS` for LAN or reverse-proxy deployments.
- No credentials are committed; configuration is environment-based.

## Deployment boundary

The MVP is a single-user local application and has no authentication. Do not expose ports 3000 or 8000 to an untrusted network. Add authentication, TLS, rate limiting, malware scanning, and a sandboxed PDF parser before multi-user or public deployment.

## Reporting

Do not include private documents, model keys, or authorization codes in issue reports. Rotate any credential accidentally posted to chat or version control.
