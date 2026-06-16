"""Grok CLI command-shape helpers and narrow JSONL output parser.

The Grok CLI emits a stream of newline-delimited JSON events.  The primary
format observed from the real binary:

    {"type": "text",  "data": "<chunk>"}            - partial text delta
    {"type": "end",   "stopReason": "EndTurn",
     "sessionId": "<id>", "usage": {...}}            - terminal event

A session-init event may appear at the start:

    {"type": "session.started", "metadata": {"sessionId": "<id>"}}

Grok may also emit a ``type:"result"`` terminal event (used in some API-relay
shapes):

    {"type": "result", "role": "system",
     "result": {"content": "<text>"}}

Usage keys emitted by the Grok CLI use both the standard Anthropic aliases
(``input_tokens``/``output_tokens``) and Grok-native aliases
(``prompt_tokens``/``completion_tokens``).  Both are handled here so that
usage accounting is correct without widening the shared ``_usage_totals_from_dict``
helper used by Claude/Codex.
"""

from __future__ import annotations

import shutil

import structlog

from agentshore.agents._jsonl import (
    _first_int,
    _iter_json_events,
    _max_usage,
    _safe_int,
    _UsageTotals,
)

_logger = structlog.get_logger(__name__)

# The installed Grok CLI (0.2.32) only accepts ``grok-build`` and
# ``grok-composer-2.5-fast`` (verified via ``grok models``). The historic
# ``grok-code-fast*`` ids are NOT valid model names, so a configured small tier
# pointing at one would be rejected by the binary — we rewrite them to the
# closest valid model (``grok-build``) and warn so the silent override is visible
# (issue #204, task 4).
_GROK_CLI_MODEL_ALIASES: dict[str, str] = {
    "grok-build-0.1": "grok-build",
    "grok-code-fast-1": "grok-build",
    "grok-code-fast": "grok-build",
    "grok-code-fast-1-0825": "grok-build",
}


def default_binary() -> str:
    """Prefer ``grok`` but support hosts that only have the ``grok-build`` alias."""
    if shutil.which("grok") is not None:
        return "grok"
    if shutil.which("grok-build") is not None:
        return "grok-build"
    return "grok"


def cli_model(model: str) -> str:
    """Return the model id accepted by the installed Grok CLI.

    Configured ids in ``_GROK_CLI_MODEL_ALIASES`` (e.g. the invalid
    ``grok-code-fast*`` family) are rewritten to a valid Grok model. The
    installed CLI rejects those ids outright, so honoring them as-is would fail
    every dispatch; rewriting is the safe option, but we emit a warning so the
    override is not silent (issue #204, task 4).
    """
    aliased = _GROK_CLI_MODEL_ALIASES.get(model)
    if aliased is not None and aliased != model:
        _logger.warning(
            "grok_model_alias_override",
            configured_model=model,
            resolved_model=aliased,
            reason="configured model is not accepted by the installed Grok CLI",
        )
        return aliased
    return model


def build_argv(
    *,
    prompt: str,
    binary: str | None,
    model: str | None,
    reasoning_effort: str | None,
    extra_flags: tuple[str, ...],
    project_dir: str | None,
    prompt_on_stdin: bool,
    prompt_file: str | None = None,
) -> list[str]:
    """Return argv for one non-interactive Grok CLI invocation.

    Unlike claude/codex/gemini, the Grok CLI has **no stdin prompt mode**: its
    ``-p/--single`` flag validates that the prompt value is non-empty before
    reading anything, so the empty ``-p ""`` headless shape the other CLIs use
    on Windows fails immediately with ``Error: --single: prompt is empty``
    (issue #160). When the caller cannot pass the prompt as an argv element
    (Windows arg-length limits), it writes the prompt to a temp file and passes
    its path as *prompt_file*; Grok reads it via ``--prompt-file``. Otherwise
    the prompt is passed directly via ``-p`` — never as an empty string.
    """
    resolved_binary = binary or default_binary()
    resolved_model = cli_model(model) if model else None
    args = [
        resolved_binary,
        "--no-auto-update",
        "--no-subagents",
        "--verbatim",
    ]
    if project_dir:
        args += ["--cwd", project_dir]
    args += ["--output-format", "streaming-json"]
    if resolved_model:
        args += ["-m", resolved_model]
    if reasoning_effort:
        args += ["--reasoning-effort", reasoning_effort]
    args.extend(extra_flags)
    if prompt_file is not None:
        args += ["--prompt-file", prompt_file]
    else:
        args += ["-p", prompt]
    return args


def _grok_usage_from_dict(usage: dict[str, object]) -> _UsageTotals:
    """Extract usage totals from a Grok CLI usage dict.

    Tolerant of every shape observed or plausibly emitted across Grok CLI
    versions, because the live binary (0.2.32) emits **no** usage block at all
    in either ``streaming-json`` or ``json`` output (verified directly — the
    terminal ``end``/``json`` event carries only ``stopReason``/``sessionId``/
    ``requestId``), so this parser is written to capture usage *if* a future
    version (or a different model/relay path) supplies it. Recognised shapes:

    - standard Anthropic keys: ``input_tokens``/``output_tokens``,
      ``cached_input_tokens``/``cache_read_input_tokens``,
      ``cache_creation_input_tokens``, ``reasoning_output_tokens``;
    - Grok/OpenAI-native aliases: ``prompt_tokens``/``completion_tokens``;
    - flat top-level aliases: ``tokens_in``/``tokens_out`` (and
      ``input``/``output``).
    """
    input_tokens = _first_int(usage, "input_tokens", "prompt_tokens", "tokens_in", "input")
    cache_read_tokens = _safe_int(usage.get("cached_input_tokens")) + _safe_int(
        usage.get("cache_read_input_tokens")
    )
    cache_write_tokens = _first_int(usage, "cache_creation_input_tokens")
    output_tokens = _first_int(usage, "output_tokens", "completion_tokens", "tokens_out", "output")
    reasoning_tokens = _first_int(usage, "reasoning_output_tokens")

    tokens_out = output_tokens if output_tokens > 0 else reasoning_tokens
    return _UsageTotals(
        tokens_in=input_tokens,
        tokens_out=tokens_out,
        cached_tokens_in=cache_read_tokens,
        cache_write_tokens_in=cache_write_tokens,
        max_turn_input_tokens=input_tokens,
    )


def _grok_usage_block(event: dict[str, object]) -> dict[str, object] | None:
    """Find a usage dict on a Grok terminal event, tolerant of nesting.

    Grok may carry usage at the top level (``usage``) or — across relay/version
    shapes — nested one level under ``result``/``message``/``response``/``turn``.
    Also accepts a flat ``tokens_in``/``tokens_out`` pair promoted onto the event
    itself. Returns ``None`` when no usage-bearing keys are present (the live
    0.2.32 case).
    """
    direct = event.get("usage")
    if isinstance(direct, dict):
        return direct
    for parent_key in ("result", "message", "response", "turn"):
        parent = event.get(parent_key)
        if isinstance(parent, dict):
            nested = parent.get("usage")
            if isinstance(nested, dict):
                return nested
    if "tokens_in" in event or "tokens_out" in event:
        return event
    return None


def _grok_session_id(event: dict[str, object]) -> str | None:
    """Extract Grok session ID from an event dict.

    Grok places the session ID in:
    - ``sessionId`` (top-level, e.g. on the ``end`` event)
    - ``session_id`` (alternative spelling)
    - ``metadata.sessionId`` (on ``session.started`` events)
    """
    for key in ("sessionId", "session_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        for key in ("sessionId", "session_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def parse_grok_jsonl(raw: str) -> tuple[str, _UsageTotals, str | None]:
    """Parse Grok CLI JSONL output into (text, usage_totals, session_id).

    Recognises the narrow real-world Grok CLI format:
    - ``type:"text"`` events contribute text chunks (from the ``data`` field).
    - ``type:"end"`` is the terminal event; carries ``sessionId`` and
      optionally ``usage``.
    - ``type:"result"`` is accepted as a fallback terminal when ``end`` is not
      present (covers API-relay output shapes).
    - ``type:"session.started"`` provides the session ID from ``metadata``.

    All other event types are ignored.
    """
    session_id: str | None = None
    usage_totals = _UsageTotals()
    text_chunks: list[str] = []
    terminal_text: str | None = None

    for event in _iter_json_events(raw):
        # Session ID: pick up from any event that carries it.
        session_id = session_id or _grok_session_id(event)

        event_type = str(event.get("type") or "").lower()

        if event_type == "text":
            # Primary streaming text delta.
            data = event.get("data")
            if isinstance(data, str):
                text_chunks.append(data)
            continue

        if event_type == "end":
            # Terminal event - extract usage and session ID (may be here).
            # NOTE: grok 0.2.32 emits NO usage on ``end`` (verified); this stays
            # for forward-compat / relay shapes that do.
            usage_raw = _grok_usage_block(event)
            if usage_raw is not None:
                usage_totals = _max_usage(usage_totals, _grok_usage_from_dict(usage_raw))
            # session_id was already updated above via _grok_session_id(event).
            continue

        if event_type == "result":
            # Fallback terminal: some API-relay shapes emit ``type:"result"``
            # instead of ``type:"end"``.  Extract the text content and usage.
            result_field = event.get("result")
            message_field = event.get("message")
            if isinstance(result_field, dict):
                content = result_field.get("content")
                if isinstance(content, str):
                    terminal_text = content
            elif isinstance(result_field, str):
                terminal_text = result_field
            if terminal_text is None and isinstance(message_field, dict):
                content = message_field.get("content")
                if isinstance(content, str):
                    terminal_text = content
            usage_raw = _grok_usage_block(event)
            if usage_raw is not None:
                usage_totals = _max_usage(usage_totals, _grok_usage_from_dict(usage_raw))
            continue

        # All other event types (session.started, assistant echoes, etc.) are
        # skipped - their data is already captured through _grok_session_id.

    assembled = "".join(text_chunks)
    return (terminal_text or assembled or raw), usage_totals, session_id
