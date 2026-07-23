"""Antigravity CLI command-shape helper (binary ``agy``).

The Antigravity CLI is invoked headless as::

    agy --model "<MODEL>" --add-dir "<project_dir>" \
        --dangerously-skip-permissions -p "<PROMPT>"

Unlike claude/codex/grok, ``agy`` emits **plain text** on stdout — there
is no ``--output-format`` flag, no JSON/JSONL stream, and therefore no per-event
usage block to parse. The dispatch layer relies on the no-parser passthrough:
because ``agy`` is deliberately absent from ``cli_agent._PARSERS``, the read loop
returns raw stdout verbatim and reports zero token usage. This module owns only
the argv shape; there is no usage/session parser here on purpose.

The reasoning effort is baked into the model display-name (e.g.
``"Gemini 3.5 Flash (Low)"``), so there is no ``--effort`` flag. ``agy`` also has
no stdin prompt mode and no prompt-file mode — the prompt is always passed as an
argv element via ``-p``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from agentshore.state import AgentType

# Terminal-control escape sequences. Under a ConPTY (the Windows spawn path —
# see ``agents/cli/conpty.py``) ``agy`` emits a terminal prelude before its real
# output, e.g. ``ESC[1t ESC[c ESC[?1004h ESC[?9001h`` (window-title stack,
# Device-Attributes query, focus reporting, win32 input mode) plus cursor/colour
# codes interleaved through the stream. These must be stripped before the result
# parser sees the text. Covers CSI (``ESC[ … final``), OSC (``ESC] … BEL/ST``),
# and the short two-byte ``ESC<char>`` forms.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_2BYTE_RE = re.compile(r"\x1b[@-Z\\-_]")
# Residual C0 control bytes to drop after escape removal (keep \n and \t).
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


# agy persists a {<abs cwd>: <conversation-uuid>} map here, keyed by the
# directory it ran in. Relative to the agy process's HOME.
_CONVERSATIONS_CACHE_RELPATH = (
    ".gemini",
    "antigravity-cli",
    "cache",
    "last_conversations.json",
)

# agy's global CLI settings file (theme, model, verbosity, …). Relative to HOME.
_SETTINGS_RELPATH = (".gemini", "antigravity-cli", "settings.json")

# #242: agy auto-backgrounds long commands and ends the ``-p`` turn narrating it is "waiting
# for the background task" (prose, no JSON). Appended to the INITIAL dispatch so the handoff
# is prevented, not just retried (verified Gemini 3.5 Flash + 3.1 Pro: ~0/4 without → ~7/8
# with; residual leak caught by ``is_async_handoff`` below). agy-only — other CLIs don't
# auto-background.
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
) -> list[str]:
    """Return argv for one non-interactive Antigravity (``agy``) invocation.

    Keyword signature mirrors ``cli_grok.build_argv`` so the ``cli_agent``
    dispatch call site stays uniform across CLI agent types. ``reasoning_effort``
    (baked into *model*), ``prompt_on_stdin``, ``prompt_file``, ``context_path``,
    and ``model_tier`` are accepted only for signature parity and are
    intentionally ignored: ``agy`` has no effort flag, no stdin prompt mode, no
    prompt-file mode, no system-prompt-file flag, and no tier_map concept.
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
    # agy's print-mode wait defaults to 5m0s — far too short for coding tasks
    # (8-15 min) and there is no documented "disable" sentinel (0s would time out
    # immediately). Pass a generous 50m ceiling so agy doesn't self-time-out
    # before AgentShore's own bounds apply; AgentShore owns the dispatch timeout
    # via the wall-clock budget and the 1800s first-byte watchdog, exactly as it
    # does for the other CLIs (Claude/Codex have no internal wall-clock). (#216)
    args += ["--print-timeout", "50m0s"]
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


# #236: agy ends its turn by deferring real work to an async/background task and
# "waiting" for it, instead of running it to completion and emitting a JSON result
# block. The first observed variant delegated to the internal ``manage_task`` tool
# ("Obtaining command output... manage_task status <id>"); a later variant phrased
# the same behaviour with no ``manage_task`` token at all ("I will pause calling
# tools and wait for the cargo clippy background task to finish"). Match the
# *behaviour* (turn ended waiting on deferred work), not one literal tool name.
# Safe to keep liberal: this only runs when no JSON result block was produced, so
# the work is already incomplete — a false positive merely uses the (correct)
# "re-run synchronously" nudge instead of the generic one.
_ASYNC_HANDOFF_MARKERS: tuple[str, ...] = (
    "manage_task",
    "pause calling tools",
    "stop calling tools",
    "background task to finish",
    "wait for the background task",
    "obtaining command output",
    "wait for notification",
    "wait for the task notification",
    # #242: real-world phrasings the original markers missed (16/16 undetected),
    # observed across Gemini 3.5 Flash + 3.1 Pro when agy auto-backgrounds a build/
    # test command and ends the turn waiting. Liberal by design — this runs only on
    # the no-JSON failure path, so a false positive merely picks the (correct)
    # "re-run synchronously" nudge.
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
    # #313: every marker above needs the literal token "background", a "notify"
    # variant, or a pronoun form ("wait for it/them/the task to ..."). The miss is
    # the *named-noun-phrase* form — "I will pause to wait for the git switch
    # command to finish." is one word away from "pause calling tools" yet went
    # undetected, so the 19-min issue_pickup that produced it was never classified.
    "pause to wait",
    "pausing to wait",
    "paused to wait",
)

# #313: the named-noun-phrase generalization the literal markers cannot express —
# "wait for the <X> command to finish", "wait for the cargo test run to complete".
# Deliberately bounded: the noun phrase may not cross a sentence or line boundary
# and is capped at 60 chars, so this matches a turn-ending wait, not ordinary prose
# that happens to contain "wait" and "finish" paragraphs apart.
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
) -> list[str]:
    """Return argv for an agy JSON-retry RESUME dispatch (``--conversation <id>``).

    Mirrors :func:`build_argv` but injects ``--conversation <id>`` so ``agy``
    re-enters the prior conversation and emits the result block it omitted.
    Narrow single-shot use only (desktop-dy2j) — not general session reuse.
    Unlike the other CLIs, ``agy`` reveals no id on stdout; the caller resolves
    it from disk via :func:`resolve_conversation_id`. *model_tier* is accepted
    only for signature parity with the shared registry and is ignored.
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
    # argv[0] is the binary; inject --conversation <id> directly after it.
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

    agy has no native JSON/structured-output mode and no per-invocation verbosity
    flag — ``verbosity`` lives only in ``<home>/.gemini/antigravity-cli/
    settings.json``. ``low`` trims the prose agy emits around its fenced JSON
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
        # Missing / unreadable / malformed → start from an empty object.
        data = {}

    if "verbosity" in data:
        return False  # respect the user's existing choice

    data["verbosity"] = "low"
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with settings_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    except OSError:
        return False
    return True
