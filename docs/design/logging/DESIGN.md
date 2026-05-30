# Logging — Functional Design

## Responsibility

AgentShore uses structured logs for debugging, auditability, tests, and post-session diagnosis. Logs are JSON objects emitted through `structlog` and standard Python logging.

## Format

NDJSON: one JSON object per line. Every entry includes `event`, `timestamp`, `level`, and optionally `session_id` and `correlation_id`.
