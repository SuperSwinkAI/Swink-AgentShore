"""GitHub Copilot CLI command-shape helpers and narrow JSONL output parser.

``copilot -p <prompt> --output-format json`` emits newline-delimited JSON
events. The shape observed from the real binary (1.0.94):

    {"type": "session.mcp_server_status_changed", ...}  - first byte, at spawn
    {"type": "assistant.message",
     "data": {"content": "<text>", "phase": "final_answer", ...}}
    {"type": "session.usage_checkpoint",
     "data": {"totalPremiumRequests": <n>,
              "accountingSnapshot": {"modelMetrics": {"<model>": {"usage": {
                  "inputTokens", "outputTokens", "cacheReadTokens",
                  "cacheWriteTokens"}}}, "lastCallInputTokens": <n>}}}
    {"type": "session.error", "data": {"errorType": "...", "message": "..."}}
    {"type": "result", "sessionId": "<uuid>", "exitCode": <rc>,
     "usage": {"premiumRequests": <n>, ...}}              - terminal event

Startup failures (unknown ``--model``, bad credentials, no auth) print to
stderr only and exit 1 with no ``result`` event; those classify through the
stderr marker tables in :mod:`agentshore.error_markers`.

Usage on a resumed session (``--resume``) is session-cumulative: the
checkpoint and ``result`` counters include the prior run's requests/tokens.
"""

from __future__ import annotations

from dataclasses import replace

import structlog

from agentshore.agents._jsonl import _iter_json_events, _safe_int, _UsageTotals

_logger = structlog.get_logger(__name__)

# ponytail: Copilot bills premium requests, not tokens. $0.04 is GitHub's
# per-premium-request overage price — the ceiling: requests covered by the
# seat's monthly allowance cost nothing, and accounts on the newer AI-credit
# billing (``totalNanoAiu`` in the checkpoint) are priced differently. Make it a
# pricebook knob if ESR cost accuracy for Copilot starts to matter.
_PREMIUM_REQUEST_USD = 0.04

# Play-declared tool names (swink-coding's vocabulary, see
# ``plays/skill_backed/code_review.py``) -> copilot 1.0.94 ``--deny-tool``
# permission kinds (``copilot help permissions``). ``write`` covers every
# file-creating/editing tool (create, edit, apply_patch) but not shell
# redirection; deny rules win even under ``--yolo``.
_COPILOT_TOOL_NAMES: dict[str, str] = {
    "write_file": "write",
    "edit_file": "write",
    "bash": "shell",
    "shell": "shell",
}


def _copilot_deny_tools(disallowed_tools: tuple[str, ...]) -> list[str]:
    """Return one ``--deny-tool=<kind>`` per mapped denial; unmapped names dropped."""
    kinds: list[str] = []
    for spec in disallowed_tools:
        kind = _COPILOT_TOOL_NAMES.get(spec)
        if kind is None:
            _logger.debug("copilot_disallowed_tool_unmapped", tool=spec)
        elif kind not in kinds:
            kinds.append(kind)
    # ``=`` form: --deny-tool is variadic and would otherwise swallow later args.
    return [f"--deny-tool={kind}" for kind in kinds]


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
    """Return argv for one non-interactive Copilot CLI invocation.

    The prompt rides ``-p <prompt>``, or — when *prompt_on_stdin* (Windows
    command-line limits) — ``-p`` is omitted and copilot reads the whole prompt
    from piped stdin (verified 1.0.94). *session_id* pins a NEW run's session
    via ``--session-id <uuid>``. *disallowed_tools* become ``--deny-tool``
    rules (see ``_COPILOT_TOOL_NAMES``). ``--no-auto-update`` keeps the binary
    from self-updating mid-session; ``--no-ask-user`` removes the interactive
    question tool so a headless run never blocks.

    *context_path*, *prompt_file* and *model_tier* are accepted only for
    ``_ArgvBuilder`` signature parity and ignored: copilot has no
    system-prompt-file flag, no prompt-file mode (stdin covers it), and no
    tier_map concept.
    """
    args = [
        binary or "copilot",
        "--output-format",
        "json",
        "--no-auto-update",
        "--no-ask-user",
    ]
    if model:
        args += ["--model", model]
    if reasoning_effort:
        args += ["--reasoning-effort", reasoning_effort]
    if session_id:
        args += ["--session-id", session_id]
    args += _copilot_deny_tools(disallowed_tools)
    if project_dir:
        args += ["-C", project_dir]
    args.extend(extra_flags)
    if not prompt_on_stdin:
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
    """Return argv for a Copilot JSON-retry RESUME dispatch (``--resume=<id>``).

    Mirrors :func:`build_argv` (YOLO *extra_flags* and the play's ``--deny-tool``
    rules are per-invocation, so both are re-passed) and injects
    ``--resume=<id>`` (``=`` form: the flag's value is optional). *session_id*
    is never forwarded — a resumed run already has an id.
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
    return [argv[0], f"--resume={resume_session_id}", *argv[1:]]


def _checkpoint_usage(data: dict[str, object]) -> _UsageTotals:
    """Sum per-model token usage from a ``session.usage_checkpoint`` payload."""
    snapshot = data.get("accountingSnapshot")
    if not isinstance(snapshot, dict):
        return _UsageTotals()
    tokens_in = tokens_out = cache_read = cache_write = 0
    metrics = snapshot.get("modelMetrics")
    for model_metrics in metrics.values() if isinstance(metrics, dict) else ():
        usage = model_metrics.get("usage") if isinstance(model_metrics, dict) else None
        if isinstance(usage, dict):
            # inputTokens already includes cacheReadTokens (input + cache_read
            # tokenDetails sum to it), matching tokens_in's "all input" meaning.
            tokens_in += _safe_int(usage.get("inputTokens"))
            tokens_out += _safe_int(usage.get("outputTokens"))
            cache_read += _safe_int(usage.get("cacheReadTokens"))
            cache_write += _safe_int(usage.get("cacheWriteTokens"))
    return _UsageTotals(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cached_tokens_in=cache_read,
        cache_write_tokens_in=cache_write,
        max_turn_input_tokens=_safe_int(snapshot.get("lastCallInputTokens")),
    )


def _premium_requests(value: object) -> float:
    """Premium-request count (fractional for discounted models), 0.0 if absent."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.0


def parse_copilot_jsonl(raw: str) -> tuple[str, _UsageTotals, str | None]:
    """Parse Copilot CLI JSONL output into (text, usage_totals, session_id).

    - Text: the last ``assistant.message`` whose ``data.phase`` is
      ``final_answer``; else the last non-empty ``assistant.message`` content
      (tool-call turns carry ``content: ""``).
    - Tokens: the last ``session.usage_checkpoint`` (cumulative).
    - Session id + premium requests: the terminal ``result`` event.
    - ``session.error`` messages, or a non-zero ``result.exitCode``, surface as
      the dispatch text when no assistant text was produced.
    """
    session_id: str | None = None
    usage = _UsageTotals()
    premium = 0.0
    final_text: str | None = None
    last_text: str | None = None
    error_text: str | None = None

    for event in _iter_json_events(raw):
        event_type = event.get("type")
        data = event.get("data")
        data = data if isinstance(data, dict) else {}

        if event_type == "assistant.message":
            content = data.get("content")
            if isinstance(content, str) and content:
                last_text = content
                if data.get("phase") == "final_answer":
                    final_text = content
        elif event_type == "session.usage_checkpoint":
            usage = _checkpoint_usage(data)
            premium = _premium_requests(data.get("totalPremiumRequests")) or premium
        elif event_type == "session.error":
            message = data.get("message")
            if isinstance(message, str) and message:
                error_text = message
        elif event_type == "result":
            sid = event.get("sessionId")
            if isinstance(sid, str) and sid:
                session_id = sid
            result_usage = event.get("usage")
            if isinstance(result_usage, dict):
                premium = _premium_requests(result_usage.get("premiumRequests")) or premium
            exit_code = _safe_int(event.get("exitCode"))
            if exit_code and error_text is None:
                error_text = f"copilot exited with code {exit_code}"

    if premium > 0:
        usage = replace(usage, reported_cost=premium * _PREMIUM_REQUEST_USD)
    return (final_text or last_text or error_text or raw), usage, session_id
