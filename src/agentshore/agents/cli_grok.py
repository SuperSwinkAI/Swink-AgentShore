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

Model selection is hard-pinned: the only accepted model is ``grok-4.5``
(``grok-build`` was the prior model name, now itself an alias that collapses
here). Any configured model that is not already ``grok-4.5`` is collapsed to
it with a warning so the override is visible in logs (issue #204, task 4).
The effort flag for the Grok CLI is ``--effort`` (NOT ``--reasoning-effort``).
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


def default_binary() -> str:
    """Prefer ``grok`` but support hosts that only have the ``grok-build`` alias."""
    if shutil.which("grok") is not None:
        return "grok"
    if shutil.which("grok-build") is not None:
        return "grok-build"
    return "grok"


def cli_model(model: str) -> str:
    """Return the model id accepted by the installed Grok CLI.

    The Grok CLI is hard-pinned to ``grok-4.5``. Any input that is not
    already ``grok-4.5`` — including the retired ``grok-build`` name — is
    collapsed to it with a warning so the override is visible in logs
    (issue #204, task 4).
    """
    if model != "grok-4.5":
        _logger.warning(
            "grok_model_alias_override",
            configured_model=model,
            resolved_model="grok-4.5",
            reason="configured model is not accepted by the installed Grok CLI",
        )
    return "grok-4.5"


def build_argv(
    *,
    prompt: str,
    binary: str | None,
    model: str | None,
    reasoning_effort: str | None,
    extra_flags: tuple[str, ...],
    context_path: str | None = None,
    project_dir: str | None,
    prompt_on_stdin: bool,
    prompt_file: str | None = None,
    model_tier: str | None = None,
) -> list[str]:
    """Return argv for one non-interactive Grok CLI invocation.

    Unlike claude/codex, the Grok CLI has **no stdin prompt mode**: its
    ``-p/--single`` flag validates that the prompt value is non-empty before
    reading anything, so the empty ``-p ""`` headless shape the other CLIs use
    on Windows fails immediately with ``Error: --single: prompt is empty``
    (issue #160). When the caller cannot pass the prompt as an argv element
    (Windows arg-length limits), it writes the prompt to a temp file and passes
    its path as *prompt_file*; Grok reads it via ``--prompt-file``. Otherwise
    the prompt is passed directly via ``-p`` — never as an empty string.

    *context_path* and *model_tier* are accepted only for signature parity
    with the shared ``cli.argv._ArgvBuilder`` registry and are ignored: the
    Grok CLI has no system-prompt-file flag and no tier_map concept.
    """
    resolved_binary = binary or default_binary()
    # Hard-pinned to grok-4.5; cli_model warns + collapses any other value.
    resolved_model = cli_model(model) if model else "grok-4.5"
    args = [
        resolved_binary,
        "--no-auto-update",
        "--no-subagents",
        "--verbatim",
        # Dispatches are ephemeral/single-turn (fresh worktree per task): memory
        # risks cross-dispatch state bleed and raised TTFB (~50s vs ~35s); plan
        # mode adds an unwanted planning round. Both off to keep TTFB inside the
        # 600s budget (#213). Web search stays enabled.
        "--no-memory",
        "--no-plan",
    ]
    if project_dir:
        args += ["--cwd", project_dir]
    args += ["--output-format", "streaming-json"]
    args += ["-m", resolved_model]
    if reasoning_effort:
        args += ["--effort", reasoning_effort]
    args.extend(extra_flags)
    if prompt_file is not None:
        args += ["--prompt-file", prompt_file]
    else:
        args += ["-p", prompt]
    return args


def build_resume_argv(
    *,
    resume_session_id: str,
    prompt: str,
    binary: str | None,
    model: str | None,
    reasoning_effort: str | None,
    extra_flags: tuple[str, ...],
    project_dir: str | None,
    prompt_on_stdin: bool,
    prompt_file: str | None = None,
    model_tier: str | None = None,
) -> list[str]:
    """Return argv for a Grok JSON-retry RESUME dispatch (``-r <id>``).

    Mirrors :func:`build_argv` but injects ``-r <session_id>`` so Grok re-enters
    the prior session and emits the result block it omitted. ``--no-memory`` is
    retained from :func:`build_argv`: session resume re-enters a persisted
    transcript and is independent of Grok's cross-session *memory* feature.
    Narrow single-shot use only (desktop-dy2j). *model_tier* is accepted only
    for signature parity with the shared registry and is ignored.
    """
    argv = build_argv(
        prompt=prompt,
        binary=binary,
        model=model,
        reasoning_effort=reasoning_effort,
        extra_flags=extra_flags,
        project_dir=project_dir,
        prompt_on_stdin=prompt_on_stdin,
        prompt_file=prompt_file,
        model_tier=model_tier,
    )
    # argv[0] is the binary; inject -r <id> directly after it.
    return [argv[0], "-r", resume_session_id, *argv[1:]]


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
        session_id = session_id or _grok_session_id(event)

        event_type = str(event.get("type") or "").lower()

        if event_type == "text":
            data = event.get("data")
            if isinstance(data, str):
                text_chunks.append(data)
            continue

        if event_type == "end":
            # NOTE: grok 0.2.32 emits NO usage on ``end`` (verified); kept for
            # forward-compat / relay shapes that do.
            usage_raw = _grok_usage_block(event)
            if usage_raw is not None:
                usage_totals = _max_usage(usage_totals, _grok_usage_from_dict(usage_raw))
            continue

        if event_type == "result":
            # Fallback terminal: some API-relay shapes emit ``result`` not ``end``.
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

        # Other event types (session.started, assistant echoes) carry only the
        # session id, already captured above.

    assembled = "".join(text_chunks)
    return (terminal_text or assembled or raw), usage_totals, session_id
