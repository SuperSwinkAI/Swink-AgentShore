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
from pathlib import Path

# agy persists a {<abs cwd>: <conversation-uuid>} map here, keyed by the
# directory it ran in. Relative to the agy process's HOME.
_CONVERSATIONS_CACHE_RELPATH = (
    ".gemini",
    "antigravity-cli",
    "cache",
    "last_conversations.json",
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
    """Return argv for one non-interactive Antigravity (``agy``) invocation.

    Keyword signature mirrors ``cli_grok.build_argv`` so the ``cli_agent``
    dispatch call site stays uniform across CLI agent types. ``reasoning_effort``
    (baked into *model*), ``prompt_on_stdin``, and ``prompt_file`` are accepted
    only for signature parity and are intentionally ignored: ``agy`` has no
    effort flag, no stdin prompt mode, and no prompt-file mode. *model* is the
    display-name string (e.g. ``"Gemini 3.5 Flash (Low)"``). *extra_flags*
    carries ``--dangerously-skip-permissions`` via the YOLO default.
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
    """
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
    "background task to finish",
    "wait for the background task",
    "obtaining command output",
    "wait for notification",
    "wait for the task notification",
)


def is_async_handoff(raw: str) -> bool:
    """Return True when agy ended its turn by deferring work to an async/background
    task and waiting on it, instead of completing the work and emitting a JSON
    result block (#236)."""
    lowered = raw.lower()
    return any(marker in lowered for marker in _ASYNC_HANDOFF_MARKERS)


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
    """Return argv for an agy JSON-retry RESUME dispatch (``--conversation <id>``).

    Mirrors :func:`build_argv` but injects ``--conversation <id>`` so ``agy``
    re-enters the prior conversation and emits the result block it omitted.
    Narrow single-shot use only (desktop-dy2j) — not general session reuse.
    Unlike the other CLIs, ``agy`` reveals no id on stdout; the caller resolves
    it from disk via :func:`resolve_conversation_id`.
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
