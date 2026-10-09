"""Grok CLI command-shape helpers and narrow JSONL output parser.

``--output-format streaming-json`` emits newline-delimited JSON events. The
shape observed from the real binary (1.0.50):

    {"type": "available_commands", "tools": [...], ...}  - tool/command roster
    {"type": "text",  "data": "<chunk>"}                  - partial text delta
    {"type": "usage", "usage": {...}}                     - per-turn usage
    {"type": "end",   "stopReason": "end_turn",
     "sessionId": "<id>", "usage": {...},
     "total_cost_usd": <float>, ...}                      - terminal event

A run that fails before starting (not signed in, unknown model, bad
``--effort``) emits only ``{"type": "error", "message": "<text>"}``.

Older/relay shapes are still accepted: a ``session.started`` event carrying
``metadata.sessionId`` and a ``type:"result"`` terminal event
(``{"result": {"content": "<text>"}}``).

Usage keys emitted by the Grok CLI use both the standard Anthropic aliases
(``input_tokens``/``output_tokens``) and Grok-native aliases
(``prompt_tokens``/``completion_tokens``).  Both are handled here so that
usage accounting is correct without widening the shared ``_usage_totals_from_dict``
helper used by Claude/Codex.

The model is passed through as configured: grok >= 1.0 offers several models
(``grok models``), and availability is resolved live by
:mod:`agentshore.agents.model_resolver` before dispatch.
The effort flag is ``--effort`` (an alias of ``--reasoning-effort``).
"""

from __future__ import annotations

import shutil
from dataclasses import replace

import structlog

from agentshore.agents._jsonl import (
    _first_int,
    _iter_json_events,
    _max_usage,
    _safe_int,
    _UsageTotals,
)

_logger = structlog.get_logger(__name__)

# Play-declared tool names (swink-coding's vocabulary, see
# ``plays/skill_backed/code_review.py``) -> grok 1.0.50 built-in tool names, as
# listed in the ``available_commands`` event. ``--disallowed-tools`` removes the
# tool from the session entirely, so the denial holds under
# ``--permission-mode bypassPermissions``.
_GROK_TOOL_NAMES: dict[str, str] = {
    "write_file": "write",
    "edit_file": "search_replace",
    "bash": "run_terminal_command",
    "shell": "run_terminal_command",
    "read_file": "read_file",
    "list_files": "list_dir",
    "search": "grep",
}


def _grok_disallowed_tools(disallowed_tools: tuple[str, ...]) -> list[str]:
    """Return ``--disallowed-tools <a,b>`` for the play's denials, or ``[]``.

    Names grok has no equivalent for (including ``tool(glob)`` specs) are
    dropped with a debug log rather than passed through to fail the dispatch.
    """
    names: list[str] = []
    for spec in disallowed_tools:
        name = _GROK_TOOL_NAMES.get(spec)
        if name is None:
            _logger.debug("grok_disallowed_tool_unmapped", tool=spec)
        elif name not in names:
            names.append(name)
    return ["--disallowed-tools", ",".join(names)] if names else []


def default_binary() -> str:
    """Prefer ``grok`` but support hosts that only have the ``grok-build`` alias."""
    if shutil.which("grok") is not None:
        return "grok"
    if shutil.which("grok-build") is not None:
        return "grok-build"
    return "grok"


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
    session_id: str | None = None,
    disallowed_tools: tuple[str, ...] = (),
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

    *session_id*, when set, pins this new run's session via ``--session-id``
    (must be a UUID that does not already exist). Grok rejects it alongside
    ``-r`` without ``--fork-session``, so :func:`build_resume_argv` never
    forwards it.

    *disallowed_tools* (the play's denials, in swink-coding tool names) is
    translated through ``_GROK_TOOL_NAMES`` into one comma-separated
    ``--disallowed-tools`` list; unmapped names are dropped.

    *context_path* and *model_tier* are accepted only for signature parity with
    the shared ``cli.argv._ArgvBuilder`` registry and are ignored: grok has no
    system-prompt-file flag and no tier_map concept.
    """
    resolved_binary = binary or default_binary()
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
    if model:
        args += ["-m", model]
    if reasoning_effort:
        args += ["--effort", reasoning_effort]
    if session_id:
        args += ["--session-id", session_id]
    args.extend(extra_flags)
    args += _grok_disallowed_tools(disallowed_tools)
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
    session_id: str | None = None,
    disallowed_tools: tuple[str, ...] = (),
) -> list[str]:
    """Return argv for a Grok JSON-retry RESUME dispatch (``-r <id>``).

    Mirrors :func:`build_argv` but injects ``-r <session_id>`` so Grok re-enters
    the prior session and emits the result block it omitted. ``--no-memory`` is
    retained from :func:`build_argv`: session resume re-enters a persisted
    transcript and is independent of Grok's cross-session *memory* feature.
    Narrow single-shot use only (desktop-dy2j). *disallowed_tools* is forwarded
    (the retry is the same play, so its denials still apply); *session_id* is
    not — ``--session-id`` names a NEW session and is rejected with ``-r``.
    *model_tier* is accepted only for signature parity and ignored.
    """
    del session_id
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
        disallowed_tools=disallowed_tools,
    )
    # argv[0] is the binary; inject -r <id> directly after it.
    return [argv[0], "-r", resume_session_id, *argv[1:]]


def _grok_usage_from_dict(usage: dict[str, object]) -> _UsageTotals:
    """Extract usage totals from a Grok CLI usage dict.

    grok 1.0.50 emits standard keys (``input_tokens``/``output_tokens``/
    ``cache_read_input_tokens``/``cache_creation_input_tokens``) on both the
    ``usage`` and ``end`` events; 0.2.32 emitted none. The other shapes are
    kept for older/relay output. Recognised shapes:

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
    itself. Returns ``None`` when no usage-bearing keys are present (the 0.2.32
    ``end`` event).
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
    - ``type:"usage"`` carries per-turn ``usage``.
    - ``type:"end"`` is the terminal event; carries ``sessionId``, ``usage``
      and the vendor-billed ``total_cost_usd`` (preferred over the static
      pricing table, as for Claude).
    - ``type:"error"`` (the run failed before producing output: not signed in,
      unknown model, bad effort) surfaces its ``message`` as the dispatch text
      when no text or ``result`` was produced, instead of the raw JSON.
    - ``type:"result"`` is accepted as a fallback terminal when ``end`` is not
      present (covers API-relay output shapes).
    - ``type:"session.started"`` provides the session ID from ``metadata``.

    All other event types are ignored.
    """
    session_id: str | None = None
    usage_totals = _UsageTotals()
    text_chunks: list[str] = []
    terminal_text: str | None = None
    error_text: str | None = None

    for event in _iter_json_events(raw):
        session_id = session_id or _grok_session_id(event)

        event_type = str(event.get("type") or "").lower()

        if event_type == "text":
            data = event.get("data")
            if isinstance(data, str):
                text_chunks.append(data)
            continue

        if event_type in ("usage", "end"):
            usage_raw = _grok_usage_block(event)
            if usage_raw is not None:
                usage_totals = _max_usage(usage_totals, _grok_usage_from_dict(usage_raw))
            reported = event.get("total_cost_usd")
            if (
                isinstance(reported, int | float)
                and not isinstance(reported, bool)
                and reported > 0
            ):
                usage_totals = replace(usage_totals, reported_cost=float(reported))
            continue

        if event_type == "error":
            message = event.get("message")
            if isinstance(message, str) and message:
                error_text = message
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
    return (terminal_text or assembled or error_text or raw), usage_totals, session_id
