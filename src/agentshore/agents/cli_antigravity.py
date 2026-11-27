"""Antigravity CLI command-shape helper and output parser (binary ``agy``).

The Antigravity CLI is invoked headless as::

    agy --model "<MODEL>" --add-dir "<project_dir>" --print-timeout 24h0m0s \
        --output-format stream-json --dangerously-skip-permissions -p "<PROMPT>"

``agy`` (verified 1.3.2) offers ``--output-format text|json|stream-json``. AgentShore
uses ``stream-json`` (NDJSON, parsed by :func:`parse_stream_json`) rather than
``json``: ``json`` prints nothing until the turn ends (text mode streams, so the
idle/first-byte watchdogs would regress), and its ``usage`` is cumulative across a
resumed conversation (double-counting the JSON-retry resume). ``stream-json``
streams per step, carries the ``conversation_id`` from the first ``init`` event,
and stamps per-step usage. Its ``result.response`` is the same agent text plain
mode printed, so the async-handoff detector (#242) and the result-block parser see
identical input. Non-JSON stdout (older agy, crashes) falls back to the plain-text
passthrough. On Windows agy runs under a ConPTY, whose terminal rendering may wrap
the long NDJSON lines, so it stays on plain text there.

The model display-name bakes in the reasoning effort (e.g.
``"Gemini 3.5 Flash (Low)"``). agy also has ``--effort`` and ``--json-schema``;
AgentShore deliberately uses neither. ``agy`` has no stdin prompt mode and no
prompt-file mode — the prompt is always passed as an argv element via ``-p``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from agentshore.agents._jsonl import _iter_json_events, _safe_int, _UsageTotals
from agentshore.state import AgentType

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_2BYTE_RE = re.compile(r"\x1b[@-Z\\-_]")
_C0_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def strip_ansi(text: str) -> str:
    """Remove terminal-control escape sequences and normalise line endings.

    Strips CSI/OSC/two-byte ANSI escapes, converts CRLF/lone-CR to LF, and drops
    leftover C0 control bytes (other than ``\\n``/``\\t``). No-op for text with no
    escapes, so it is safe to run unconditionally on every agy result (POSIX
    output, which has no PTY prelude, passes through unchanged apart from CRLF
    normalisation).
    """
    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_CSI_RE.sub("", text)
    text = _ANSI_2BYTE_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _C0_CTRL_RE.sub("", text)


_CONVERSATIONS_CACHE_RELPATH = (
    ".gemini",
    "antigravity-cli",
    "cache",
    "last_conversations.json",
)

_SETTINGS_RELPATH = (".gemini", "antigravity-cli", "settings.json")

_ANTIGRAVITY_SYNCHRONOUS_DIRECTIVE = (
    "\n\n## Antigravity: run every command synchronously\n\n"
    "Run every shell command in the FOREGROUND and BLOCK until it returns, no matter how "
    "long it takes. Do NOT send commands to the background, do NOT use a task or "
    "manage_task tool, and do NOT pause to 'wait for a background task to finish' or "
    "'wait for a notification' — there is no scheduler that will wake you up. Do NOT end "
    "your turn until every command has returned and you have emitted the fenced JSON "
    "result block."
)


def decorate_initial_prompt(prompt: str, agent_type: AgentType | None) -> str:
    """Append the agy synchronous-execution directive, no-op for every other vendor.

    Single entry point the dispatch base class calls without knowing the vendor —
    it decorates the INITIAL prompt only (the resume/retry prompts have their own
    per-defect wording chosen by the caller).
    """
    if agent_type == AgentType.ANTIGRAVITY:
        return prompt + _ANTIGRAVITY_SYNCHRONOUS_DIRECTIVE
    return prompt


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
    """Return argv for one non-interactive Antigravity (``agy``) invocation.

    Keyword signature mirrors ``cli_grok.build_argv`` so the ``cli_agent``
    dispatch call site stays uniform across CLI agent types. ``reasoning_effort``
    (baked into *model*), ``prompt_on_stdin``, ``prompt_file``, ``context_path``,
    ``model_tier``, ``session_id`` and ``disallowed_tools`` are accepted only for
    signature parity and are intentionally ignored: effort rides the model
    display-name (agy's ``--effort`` is unused), and agy has no stdin prompt mode,
    no prompt-file mode, no system-prompt-file flag, no tier_map concept, no way to
    pin a new conversation's id, and no per-dispatch tool-permission flag.
    *model* is the display-name string (e.g. ``"Gemini 3.5 Flash (Low)"``).
    *extra_flags* carries ``--dangerously-skip-permissions`` via the YOLO
    default.
    """
    resolved_binary = binary or "agy"
    args = [resolved_binary]
    if model:
        args += ["--model", model]
    if project_dir:
        args += ["--add-dir", project_dir]
    args += ["--print-timeout", "24h0m0s"]
    if sys.platform != "win32":
        args += ["--output-format", "stream-json"]
    args.extend(extra_flags)
    args += ["-p", prompt]
    return args


def extract_output(raw: str) -> str:
    """Extract the actual agent output from an agy task-status block.

    When agy uses its async task system it emits:
        [Task <id>/task-N Status Update]
        Status: COMPLETED
        Exit Code: 0
        Log Path: file:///...
        Output:
        <actual model output>
        Error: <message or "(none)">

    ``parse_skill_result`` needs the content of the ``Output:`` section, not
    the wrapper. When the block is absent (streaming mode), returns *raw* unchanged.
    ``(empty)`` output is normalised to an empty string so the caller gets a
    clean ``no valid result block`` error rather than trying to parse "(empty)".

    Always strips terminal-control escapes first (:func:`strip_ansi`): under the
    Windows ConPTY spawn path agy's stdout carries a terminal prelude and
    interleaved cursor/colour codes that would otherwise corrupt both the
    task-block detection here and the downstream JSON result parse.
    """
    raw = strip_ansi(raw)
    if "[Task " not in raw or "Status Update]" not in raw:
        return raw

    output_marker = "\nOutput:\n"
    error_marker = "\nError:"
    start = raw.find(output_marker)
    if start == -1:
        return raw
    content_start = start + len(output_marker)
    end = raw.find(error_marker, content_start)
    content = raw[content_start:end].strip() if end != -1 else raw[content_start:].strip()
    return "" if content == "(empty)" else content


def parse_stream_json(raw: str) -> tuple[str, _UsageTotals, str | None]:
    """Parse ``agy --output-format stream-json`` into (text, usage, conversation id).

    Verified shape (agy 1.3.2), one JSON object per line, keyed by ``event``:

    * ``{"event":"init","conversation_id":…,"init":{…}}``
    * ``{"event":"step_update","step_update":{"conversation_id":…,"step_type":
      "agent_response"|"tool"|…,"state":"ACTIVE"|"DONE","text_delta":…,
      "usage":{input_tokens,output_tokens,thinking_tokens,cache_read_tokens,…}}}``
      — ``usage`` appears on each DONE model step and is per step.
    * ``{"event":"result","result":{"conversation_id":…,"status":"SUCCESS"|"ERROR",
      "response":…,"usage":{…},"error":…}}`` — ``usage`` here is cumulative over
      the whole conversation (a ``--conversation`` resume re-counts prior turns),
      so per-invocation usage sums the step usages instead.

    Text is ``result.response`` (falling back to the joined ``agent_response``
    deltas when the run was cut off before ``result``) — the same text plain mode
    printed, so an empty response stays empty for no-op detection. Stdout with no
    agy events at all (older agy, crash output) is returned verbatim with zero
    usage: the pre-JSON plain-text behaviour.
    """
    conversation_id: str | None = None
    deltas: list[str] = []
    response: str | None = None
    step_usages: list[dict[str, object]] = []
    result_usage: dict[str, object] | None = None
    saw_event = False

    for event in _iter_json_events(strip_ansi(raw)):
        kind = event.get("event")
        payload = event.get(kind) if isinstance(kind, str) else None
        if not isinstance(payload, dict):
            continue
        saw_event = True
        cid = event.get("conversation_id") or payload.get("conversation_id")
        if conversation_id is None and isinstance(cid, str) and cid:
            conversation_id = cid
        usage = payload.get("usage")
        if kind == "step_update":
            if payload.get("step_type") == "agent_response":
                delta = payload.get("text_delta")
                if isinstance(delta, str):
                    deltas.append(delta)
            if payload.get("state") == "DONE" and isinstance(usage, dict):
                step_usages.append(usage)
        elif kind == "result":
            text = payload.get("response")
            response = text if isinstance(text, str) else ""
            if isinstance(usage, dict):
                result_usage = usage

    if not saw_event:
        return raw, _UsageTotals(), None

    turns = step_usages or ([result_usage] if result_usage else [])
    inputs = [_safe_int(u.get("input_tokens")) for u in turns]
    totals = _UsageTotals(
        tokens_in=sum(inputs),
        tokens_out=sum(
            _safe_int(u.get("output_tokens")) + _safe_int(u.get("thinking_tokens")) for u in turns
        ),
        cached_tokens_in=sum(_safe_int(u.get("cache_read_tokens")) for u in turns),
        turn_count=len(turns),
        max_turn_input_tokens=max(inputs, default=0),
    )
    text = response if response is not None else "".join(deltas)
    return text, totals, conversation_id


_ASYNC_HANDOFF_MARKERS: tuple[str, ...] = (
    "manage_task",
    "pause calling tools",
    "stop calling tools",
    "background task to finish",
    "wait for the background task",
    "obtaining command output",
    "wait for notification",
    "wait for the task notification",
    "in the background",
    "to the background",
    "for the background",
    "wait for it to finish",
    "wait for it to complete",
    "wait for them to complete",
    "wait for the task to complete",
    "wait for the system to notify",
    "notify me when",
    "we will be notified",
    "notified upon its completion",
    "pause to wait",
    "pausing to wait",
    "paused to wait",
)

_ASYNC_HANDOFF_NAMED_WAIT_RE = re.compile(
    r"wait for (?:the|this|that|my|our)\s[^.\n]{0,60}?\bto (?:finish|complete|return|be done)\b"
)


def is_async_handoff(raw: str) -> bool:
    """Return True when agy ended its turn by deferring work to an async/background
    task and waiting on it, instead of completing the work and emitting a JSON
    result block (#236)."""
    lowered = raw.lower()
    if any(marker in lowered for marker in _ASYNC_HANDOFF_MARKERS):
        return True
    return _ASYNC_HANDOFF_NAMED_WAIT_RE.search(lowered) is not None


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
    """Return argv for an agy JSON-retry RESUME dispatch (``--conversation <id>``).

    Mirrors :func:`build_argv` but injects ``--conversation <id>`` so ``agy``
    re-enters the prior conversation and emits the result block it omitted.
    Narrow single-shot use only (desktop-dy2j) — not general session reuse.
    The id comes from the stream-json ``conversation_id`` (:func:`parse_stream_json`),
    or from disk via :func:`resolve_conversation_id` for plain-text runs. A resume
    keeps ``--output-format stream-json`` (verified). *model_tier*, *session_id*
    and *disallowed_tools* are accepted only for signature parity with the shared
    registry and are ignored — ``agy`` has no per-dispatch tool-permission flag.
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
    return [argv[0], "--conversation", resume_session_id, *argv[1:]]


def resolve_conversation_id(cwd: Path | str, *, home: str | None) -> str | None:
    """Resolve the agy conversation UUID for a dispatch worktree, or ``None``.

    ``agy`` persists a ``{<abs cwd>: <conversation-uuid>}`` map at
    ``<home>/.gemini/antigravity-cli/cache/last_conversations.json`` (keyed by
    the directory it ran in). AgentShore dispatches each agy run in its own
    unique worktree, so this lookup is per-worktree and unambiguous under a
    parallel fleet. Used to give agy a resumable session id for the narrow
    JSON-retry path (desktop-dy2j).

    Fully defensive: a missing / unreadable / malformed cache, or an absent key,
    returns ``None`` (the play then fails exactly as before — no retry).
    """
    base = home or os.environ.get("HOME") or str(Path.home())
    cache_path = Path(base, *_CONVERSATIONS_CACHE_RELPATH)
    try:
        with cache_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(str(cwd))
    return value if isinstance(value, str) and value else None


def ensure_low_verbosity_setting(*, home: str | None = None) -> bool:
    """Set ``verbosity: "low"`` in agy's global settings, preserving other keys.

    agy has no per-invocation verbosity flag — ``verbosity`` lives only in
    ``<home>/.gemini/antigravity-cli/settings.json`` (stream-json wraps, but does
    not trim, the agent's prose). ``low`` trims the prose agy emits around its fenced JSON
    result block (often to *zero* preamble), which lowers token cost and the odds
    the result parser latches onto a stray example object. AgentShore drives agy
    via the same global CLI config, so provisioning sets this once at ``init``.

    Idempotent and conservative:

    * Respects an existing ``verbosity`` value (never overwrites a user choice).
    * Preserves every other key (``colorScheme``, ``model``, …).
    * Creates the file/dirs if absent; tolerates a missing or malformed file by
      starting from an empty settings object.

    Returns ``True`` when the file was (re)written, ``False`` when it was already
    set or the write failed. Never raises.
    """
    base = home or os.environ.get("HOME") or str(Path.home())
    settings_path = Path(base, *_SETTINGS_RELPATH)

    data: dict[str, object] = {}
    try:
        with settings_path.open(encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}

    if "verbosity" in data:
        return False

    data["verbosity"] = "low"
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with settings_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    except OSError:
        return False
    return True
