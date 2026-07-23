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

A per-dispatch tier backend override is supported via ``--tier-map
<tier>=<provider>:<model>[@endpoint]`` (SuperSwink-Coding#282, shipped in
swink-coding v0.2.1). ``model`` here may therefore be either a plain tier
alias (``small``/``medium``/``large``, passed straight through as before) or
a ``provider:model[@endpoint]`` string, which is routed through
``--tier-map`` instead — see :func:`classify_swink_model`.

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

from typing import Literal

from agentshore.agents._jsonl import (
    _iter_json_events,
    _max_usage,
    _usage_totals_from_dict,
    _UsageTotals,
)

# The three tier aliases the installed binary's ``--model`` flag accepts
# directly. Any other value must be a ``provider:model[@endpoint]`` tier_map
# override — see :func:`classify_swink_model`.
TIER_ALIASES: frozenset[str] = frozenset({"small", "medium", "large"})

_INVALID_MODEL_MSG = (
    "invalid swink-coding model {value!r}: valid forms are a tier alias "
    "(small, medium, large) or provider:model[@endpoint]"
)


def classify_swink_model(value: str) -> Literal["alias", "tier_map"]:
    """Classify *value* as a tier alias or a ``provider:model[@endpoint]`` override.

    - ``"alias"``: *value* is one of :data:`TIER_ALIASES` — passed straight
      through as ``--model <value>`` (unchanged behavior).
    - ``"tier_map"``: *value* matches ``provider:model[@endpoint]``, where
      *provider* is a nonempty token containing neither ``:`` nor ``=``, and
      *model* is nonempty and may itself contain ``:`` (e.g.
      ``ollama:qwen2.5-coder:7b``). A trailing ``@...`` suffix counts as an
      *endpoint* only when the part after ``@`` contains ``://`` (e.g.
      ``vllm:m@http://host:8000/v1``); otherwise the ``@`` folds into the
      model id verbatim, per the upstream grammar (SuperSwink-Coding#282).

    Raises ``ValueError`` for anything else — most commonly a bare model id
    with no ``provider:`` prefix (e.g. ``"gpt-4"``), an empty provider
    (``":model"``), or an empty model (``"ollama:"``).
    """
    if value in TIER_ALIASES:
        return "alias"

    if ":" not in value:
        raise ValueError(_INVALID_MODEL_MSG.format(value=value))

    provider, rest = value.split(":", 1)
    if not provider or "=" in provider:
        raise ValueError(_INVALID_MODEL_MSG.format(value=value))

    model_part = rest
    if "@" in rest:
        candidate_model, _, candidate_endpoint = rest.partition("@")
        if "://" in candidate_endpoint:
            model_part = candidate_model
        # else: no URL scheme after '@' — it's part of the model id, not an
        # endpoint suffix; model_part stays as the full `rest`.

    if not model_part:
        raise ValueError(_INVALID_MODEL_MSG.format(value=value))

    return "tier_map"


def _model_flags(model: str | None, model_tier: str | None) -> list[str]:
    """Return the ``--model``/``--tier-map`` argv slice for *model*.

    A tier alias emits ``--model <alias>`` unchanged. A ``provider:model[@endpoint]``
    tier_map override additionally requires *model_tier* (the alias whose backend
    is being overridden for this one dispatch) and emits both
    ``--model <model_tier>`` and ``--tier-map <model_tier>=<model>``.
    """
    if not model:
        return []
    if classify_swink_model(model) == "alias":
        return ["--model", model]
    if model_tier is None or model_tier not in TIER_ALIASES:
        msg = (
            f"swink-coding tier_map model {model!r} requires model_tier to be one "
            f"of {sorted(TIER_ALIASES)}, got {model_tier!r}"
        )
        raise ValueError(msg)
    return ["--model", model_tier, "--tier-map", f"{model_tier}={model}"]


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
    """Return argv for one non-interactive swink-coding CLI invocation.

    Keyword signature mirrors ``cli_grok.build_argv``/``cli_antigravity.build_argv``
    so the ``cli_agent`` dispatch call site stays uniform across CLI agent types.
    ``reasoning_effort`` and ``context_path`` are accepted only for signature
    parity and are intentionally ignored: the installed binary registers no
    efforts for this agent type (no ``--effort`` flag is ever emitted) and has
    no system-prompt-file flag. *extra_flags* carries ``--yolo`` via the
    YOLO default. Prompt delivery has three mutually-exclusive modes: *prompt_file*
    (``--prompt-file <path>``, the Windows/large-prompt path) takes priority; else
    stdin (*prompt_on_stdin*, ``-p`` omitted entirely — the child reads the whole
    prompt from stdin); else the prompt rides directly as a ``-p`` argv element.

    *model* may be a tier alias (``small``/``medium``/``large``, emitted as
    ``--model <value>`` unchanged) or a ``provider:model[@endpoint]`` per-dispatch
    tier_map override (SuperSwink-Coding#282), in which case *model_tier* (the
    tier alias being overridden) is required and both ``--model <model_tier>``
    and ``--tier-map <model_tier>=<value>`` are emitted. See
    :func:`classify_swink_model`.
    """
    resolved_binary = binary or "swink-coding"
    args = [resolved_binary]
    args += _model_flags(model, model_tier)
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
    model_tier: str | None = None,
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
        model_tier=model_tier,
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
