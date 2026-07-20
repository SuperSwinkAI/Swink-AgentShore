"""swink-coding CLI command-shape helpers and NDJSON output parser.

The swink-coding CLI is invoked headless as::

    swink-coding -p "<PROMPT>" --model <small|medium|large> --yolo \
        --output-format stream-json --cwd "<project_dir>"

Unlike Grok, swink-coding has both a stdin prompt mode (omit ``-p`` entirely)
and a ``--prompt-file <path>`` mode for the Windows/large-prompt path — no
temp-file workaround is required. ``--model`` accepts *only* the tier alias
strings ``small``/``medium``/``large`` (a raw model id is rejected by the
installed binary); AgentShore's model-tier config already carries these
literal aliases as the resolved model, so *model* is passed straight through
with no translation. There is no supported reasoning-effort flag for this CLI
(the binary exposes ``--effort`` but AgentShore registers no efforts for this
type), so *reasoning_effort* is accepted only for signature parity with the
other CLI adapters and is otherwise ignored.

Output is newline-delimited JSON on stdout::

    {"type":"session.started","session_id":"sc_...","tier":"small","context_window":32768}
    {"type":"tool_use","name":"bash","input":{...}}
    {"type":"tool_result","name":"bash","ok":true}
    {"type":"text","data":"<delta>"}
    {"type":"result","text":"<FULL final text>","tier":"small",
     "context_window":32768,"session_id":"sc_...","usage":{...},
     "duration_ms":N,"time_to_first_byte_ms":N|null,"empty":true?}
    {"type":"error","message":"..."}

``result`` is the terminal event and carries the authoritative final text,
usage, and session id — it is preferred over concatenating ``text`` deltas.
``error`` is emitted (with a non-zero process exit) when the run fails before
a ``result`` event is produced; its ``message`` is surfaced as the dispatch
text so downstream failure handling sees it, mirroring how the other CLI
parsers fall back when no terminal success event arrives.

Usage keys (``input_tokens``/``output_tokens``/``cache_read_input_tokens``/
``cache_creation_input_tokens``/``reasoning_output_tokens``) already match the
shared Anthropic-style shape ``_usage_totals_from_dict`` understands, so no
swink-coding-specific usage mapper is needed (unlike Grok's aliased keys).
"""

from __future__ import annotations

from agentshore.agents._jsonl import (
    _iter_json_events,
    _max_usage,
    _usage_totals_from_dict,
    _UsageTotals,
)


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
    """Return argv for one non-interactive swink-coding CLI invocation.

    Keyword signature mirrors ``cli_grok.build_argv``/``cli_antigravity.build_argv``
    so the ``cli_agent`` dispatch call site stays uniform across CLI agent types.
    ``reasoning_effort`` is accepted only for signature parity and is intentionally
    ignored: the installed binary registers no efforts for this agent type, so no
    ``--effort`` flag is ever emitted. *extra_flags* carries ``--yolo`` via the
    YOLO default. Prompt delivery has three mutually-exclusive modes: *prompt_file*
    (``--prompt-file <path>``, the Windows/large-prompt path) takes priority; else
    stdin (*prompt_on_stdin*, ``-p`` omitted entirely — the child reads the whole
    prompt from stdin); else the prompt rides directly as a ``-p`` argv element.
    """
    resolved_binary = binary or "swink-coding"
    args = [resolved_binary]
    if model:
        args += ["--model", model]
    args.extend(extra_flags)
    args += ["--output-format", "stream-json"]
    if project_dir:
        args += ["--cwd", project_dir]
    if prompt_file is not None:
        args += ["--prompt-file", prompt_file]
    elif not prompt_on_stdin:
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
) -> list[str]:
    """Return argv for a swink-coding JSON-retry RESUME dispatch (``--resume <id>``).

    Mirrors :func:`build_argv` but injects ``--resume <session_id>`` so
    swink-coding re-enters the prior session and emits the ``result`` event it
    omitted. Narrow single-shot use only (desktop-dy2j), matching the other CLI
    adapters' resume shape. Always uses the explicit session id, never the
    ``latest`` sentinel the binary also accepts.
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
    )
    # argv[0] is the binary; inject --resume <id> directly after it.
    return [argv[0], "--resume", resume_session_id, *argv[1:]]


def _swink_coding_session_id(event: dict[str, object]) -> str | None:
    """Extract the session id from a swink-coding event, when present.

    Both ``session.started`` and the terminal ``result`` event carry a
    top-level ``session_id`` string.
    """
    value = event.get("session_id")
    return value if isinstance(value, str) and value else None


def parse_swink_coding_jsonl(raw: str) -> tuple[str, _UsageTotals, str | None]:
    """Parse swink-coding CLI NDJSON output into (text, usage_totals, session_id).

    - ``type:"result"`` is the terminal event: its ``text`` field (even when
      empty — the CLI flags a legitimately empty result via ``"empty":true``)
      is authoritative and is never overridden by concatenated deltas.
    - ``type:"error"`` is used as a fallback terminal only when no ``result``
      event ever arrived (the run failed before producing one); its ``message``
      is surfaced as the dispatch text.
    - ``type:"text"`` deltas are accumulated and used only when neither a
      ``result`` nor an ``error`` event was seen, with *raw* as the final
      fallback.
    - ``type:"session.started"``/``tool_use``/``tool_result`` contribute only
      the session id (from ``session.started``), already handled generically.
    """
    session_id: str | None = None
    usage_totals = _UsageTotals()
    text_chunks: list[str] = []
    result_seen = False
    terminal_text: str | None = None
    error_text: str | None = None

    for event in _iter_json_events(raw):
        session_id = session_id or _swink_coding_session_id(event)

        event_type = str(event.get("type") or "")

        if event_type == "text":
            data = event.get("data")
            if isinstance(data, str):
                text_chunks.append(data)
            continue

        if event_type == "result":
            result_seen = True
            text_value = event.get("text")
            if isinstance(text_value, str):
                terminal_text = text_value
            usage = event.get("usage")
            if isinstance(usage, dict):
                usage_totals = _max_usage(
                    usage_totals, _usage_totals_from_dict(usage, input_includes_cache=False)
                )
            continue

        if event_type == "error":
            message = event.get("message")
            if isinstance(message, str):
                error_text = message
            continue

        # session.started / tool_use / tool_result: session id (if any) is
        # already captured above; these carry no text or usage of their own.

    if result_seen:
        text_out = terminal_text if terminal_text is not None else ""
    elif error_text is not None:
        text_out = error_text
    else:
        text_out = "".join(text_chunks) or raw

    return text_out, usage_totals, session_id
