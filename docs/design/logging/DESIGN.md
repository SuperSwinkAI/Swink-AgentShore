# Logging — Functional Design

## Responsibility

AgentShore emits structured logs for debugging, auditability, tests, and post-session diagnosis. Logging is configured once in `src/agentshore/logging.py` (`setup_logging`), which wires `structlog` on top of stdlib logging so every module obtains a bound logger via `get_logger`.

Cross-references: [HLD](../HLD.md) lists this component; failure classification consumed downstream lives in [errors](../errors/DESIGN.md).

## Design Choices

- **Structured NDJSON, not formatted text.** All output is rendered as one JSON object per line (`JSONRenderer`). The rationale is machine-parseability: tests assert on event fields, and a finished session's log can be grepped or queried for diagnosis without a custom parser. There is no human-pretty console renderer — even interactive runs emit NDJSON.
- **Context is injected, not threaded by hand.** `session_id` and `correlation_id` ride in `contextvars`, so processors stamp them onto every entry automatically rather than each call site passing them. `with_correlation()` binds a correlation id for an enclosed scope. This keeps related entries joinable across async tasks without polluting every logging call.
- **Tracebacks survive into the JSON.** A `dict_tracebacks` processor runs before the JSON renderer so `exc_info`/`.exception(...)` sites emit a real structured traceback instead of a bare `"exc_info": true`. This exists because a dropped stack once made an orchestrator-loop crash undiagnosable.
- **Single configuration path, dual sinks.** One processor chain feeds both a stderr handler and (when `log_dir` + `session_id` are supplied) a per-session file handler at `<log_dir>/agentshore-<session_id>.log`. Level is one of `debug`/`info`/`warning`/`error`, applied via a filtering bound logger.

## Event Model

Every entry carries an `event` name, an ISO `timestamp`, and a `level`. When bound, `session_id` and `correlation_id` are added automatically; failure paths additionally carry a structured `exception` field. Event names are free-form strings chosen at the call site (e.g. `metrics_query_failed`, `epic_metrics`).
