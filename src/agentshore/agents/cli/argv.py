"""Argv construction helpers for the CLI agent adapter.

Builds the subprocess argument list for each supported CLI agent type. Extracted
from ``cli_agent`` to give argv logic a focused home while keeping ``cli_agent``
as the driver.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Protocol

from agentshore.state import AgentType

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------


def _prompt_on_stdin(python_executable: str | None) -> bool:
    """Return True when Windows npm shims should receive the prompt over stdin."""
    import sys

    return python_executable is None and sys.platform == "win32"


def _resolve_executable(argv: list[str]) -> list[str]:
    """On Windows resolve argv[0] to its full path so .cmd/.bat shims run.

    ``subprocess_env.resolve_tool`` is for known tools (git/gh); here we need
    the same PATHEXT-aware which() for arbitrary agent CLI names (claude.cmd,
    codex.cmd, agy.cmd) that are npm shims on Windows.
    """
    import os
    import shutil
    import sys

    if sys.platform != "win32" or not argv or os.path.isabs(argv[0]):
        return argv
    resolved = shutil.which(argv[0])
    if resolved is None:
        return argv
    return [resolved, *argv[1:]]


def _write_grok_prompt_file(prompt: str) -> Path:
    """Write *prompt* to a temp file for Grok's ``--prompt-file`` (issue #160).

    Grok has no stdin prompt mode and rejects an empty ``-p`` value, so on
    Windows — where we can't pass a large prompt as an argv element — the
    prompt is delivered through a file instead. The caller owns cleanup
    (``unlink`` in its ``finally``).
    """
    import os
    import tempfile
    from pathlib import Path

    fd, path = tempfile.mkstemp(prefix="agentshore-grok-prompt-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return Path(path)


# ---------------------------------------------------------------------------
# YOLO defaults
# ---------------------------------------------------------------------------

_DEFAULT_YOLO_FLAGS: dict[AgentType, tuple[str, ...]] = {
    AgentType.CLAUDE_CODE: ("--dangerously-skip-permissions",),
    AgentType.CODEX: (
        "--ignore-user-config",
        "--ignore-rules",
        "--dangerously-bypass-approvals-and-sandbox",
    ),
    AgentType.GROK: ("--permission-mode", "bypassPermissions"),
    AgentType.ANTIGRAVITY: ("--dangerously-skip-permissions",),
    AgentType.SWINK_CODING: ("--yolo",),
}

# Agent types whose CLI exposes a resume-by-id flag AND for which AgentShore
# holds a stable session id (claude/codex/grok/swink-coding parse it from
# stdout; agy resolves it from its on-disk conversation cache). The narrow
# JSON-retry path (desktop-dy2j) re-enters that one session to recover the
# omitted result block.
_RESUMABLE_AGENT_TYPES: frozenset[AgentType] = frozenset(
    {
        AgentType.CLAUDE_CODE,
        AgentType.CODEX,
        AgentType.GROK,
        AgentType.ANTIGRAVITY,
        AgentType.SWINK_CODING,
    }
)


def _apply_yolo_default(agent_type: AgentType, extra_flags: tuple[str, ...]) -> tuple[str, ...]:
    """Return YOLO defaults for *agent_type* when the user provided no flags."""
    if extra_flags:
        return extra_flags
    return _DEFAULT_YOLO_FLAGS.get(agent_type, ())


# ---------------------------------------------------------------------------
# Builder registry
#
# Every per-type builder shares one keyword-only shape (a plain function for
# claude/codex, module-level ``build_argv``/``build_resume_argv`` for
# grok/antigravity/swink-coding) so the two dispatchers below are a dict
# lookup, not an if/elif chain — the same registry treatment
# ``cli/parsing.py``'s ``_PARSERS`` already uses for output parsing. A builder
# ignores whichever keywords its CLI doesn't support; that's documented on the
# builder itself, not here.
# ---------------------------------------------------------------------------


class _ArgvBuilder(Protocol):
    """Shape every entry in ``_ARGV_BUILDERS`` matches."""

    def __call__(
        self,
        *,
        prompt: str,
        binary: str | None,
        model: str | None,
        reasoning_effort: str | None,
        extra_flags: tuple[str, ...],
        context_path: str | None,
        project_dir: str | None,
        prompt_on_stdin: bool,
        prompt_file: str | None,
        model_tier: str | None,
    ) -> list[str]: ...


class _ResumeArgvBuilder(Protocol):
    """Shape every entry in ``_RESUME_ARGV_BUILDERS`` matches."""

    def __call__(
        self,
        *,
        resume_session_id: str,
        prompt: str,
        binary: str | None,
        model: str | None,
        reasoning_effort: str | None,
        extra_flags: tuple[str, ...],
        project_dir: str | None,
        prompt_on_stdin: bool,
        prompt_file: str | None,
        model_tier: str | None,
    ) -> list[str]: ...


def _build_argv_claude_code(
    *,
    prompt: str,
    binary: str | None,
    model: str | None,
    reasoning_effort: str | None,
    extra_flags: tuple[str, ...],
    context_path: str | None,
    project_dir: str | None,
    prompt_on_stdin: bool,
    prompt_file: str | None,
    model_tier: str | None,
) -> list[str]:
    """Claude Code argv. ``project_dir``, ``prompt_file``, and ``model_tier``
    are accepted only for ``_ArgvBuilder`` signature parity and ignored:
    Claude has no working-directory flag, no prompt-file mode, and no
    tier_map concept.
    """
    binary = binary or "claude"
    args = [binary, "-p", "--verbose", "--output-format", "stream-json"]
    if model:
        args += ["--model", model]
    if reasoning_effort:
        args += ["--effort", reasoning_effort]
    args.extend(extra_flags)
    if context_path:
        args += ["--append-system-prompt", f"Context file: {context_path}"]
    if not prompt_on_stdin:
        args.append(prompt)
    return args


def _build_argv_codex(
    *,
    prompt: str,
    binary: str | None,
    model: str | None,
    reasoning_effort: str | None,
    extra_flags: tuple[str, ...],
    context_path: str | None,
    project_dir: str | None,
    prompt_on_stdin: bool,
    prompt_file: str | None,
    model_tier: str | None,
) -> list[str]:
    """Codex argv. ``context_path``, ``prompt_file``, and ``model_tier`` are
    accepted only for ``_ArgvBuilder`` signature parity and ignored: Codex has
    no system-prompt-file flag, no prompt-file mode, and no tier_map concept.
    """
    binary = binary or "codex"
    yolo = "--dangerously-bypass-approvals-and-sandbox" in extra_flags
    args = [binary, "exec", "--json"]
    if not yolo:
        args.append("--full-auto")
    if model:
        args += ["-m", model]
    if reasoning_effort:
        args += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
    # desktop-pxg: codex's shell tool otherwise strips the env, so our
    # injected GH_TOKEN/GH_CONFIG_DIR never reach `gh api user` (identity
    # mismatch, refused mutations). inherit=all passes them through.
    args += ["-c", "shell_environment_policy.inherit=all"]
    args.extend(extra_flags)
    if project_dir:
        args += ["-C", project_dir]
    # "-" tells `codex exec` to read the prompt from stdin.
    args.append("-" if prompt_on_stdin else prompt)
    return args


def _build_resume_argv_claude_code(
    *,
    resume_session_id: str,
    prompt: str,
    binary: str | None,
    model: str | None,
    reasoning_effort: str | None,
    extra_flags: tuple[str, ...],
    project_dir: str | None,
    prompt_on_stdin: bool,
    prompt_file: str | None,
    model_tier: str | None,
) -> list[str]:
    """Claude Code resume argv. ``model``, ``reasoning_effort``, ``extra_flags``,
    ``project_dir``, ``prompt_file``, and ``model_tier`` are accepted only for
    ``_ResumeArgvBuilder`` signature parity and ignored: ``--resume`` re-enters
    the prior session verbatim with no per-dispatch flags.
    """
    binary = binary or "claude"
    argv = [
        binary,
        "--resume",
        resume_session_id,
        "-p",
        "--verbose",
        "--output-format",
        "stream-json",
    ]
    if not prompt_on_stdin:
        argv.append(prompt)
    return argv


def _build_resume_argv_codex(
    *,
    resume_session_id: str,
    prompt: str,
    binary: str | None,
    model: str | None,
    reasoning_effort: str | None,
    extra_flags: tuple[str, ...],
    project_dir: str | None,
    prompt_on_stdin: bool,
    prompt_file: str | None,
    model_tier: str | None,
) -> list[str]:
    """Codex resume argv — splices ``exec resume <id>`` into the base
    :func:`_build_argv_codex` argv (issue #329, see the ``-C`` strip below)."""
    base = _build_argv_codex(
        prompt=prompt,
        binary=binary,
        model=model,
        reasoning_effort=reasoning_effort,
        extra_flags=extra_flags,
        context_path=None,
        project_dir=project_dir,
        prompt_on_stdin=prompt_on_stdin,
        prompt_file=prompt_file,
        model_tier=model_tier,
    )
    # Splice "resume <id>" into base [binary, "exec", "--json", ...].
    # `codex exec resume` (unlike `codex exec`) does not accept the `-C
    # <dir>` working-directory flag and exits 2 with "unexpected argument
    # '-C' found" if it's present (issue #329). Strip that flag/value pair
    # from the tail before splicing — the subprocess is already launched
    # with cwd=effective_cwd (see cli_agent._build_dispatch_argv callers),
    # so `-C` is redundant here, not merely unsupported. Only the exact
    # standalone `-C` token is matched (case-sensitive), so the unrelated
    # lowercase `-c model_reasoning_effort=...` / `-c
    # shell_environment_policy...` config flags are left untouched.
    tail = base[2:]
    filtered_tail: list[str] = []
    skip_next = False
    for arg in tail:
        if skip_next:
            skip_next = False
            continue
        if arg == "-C":
            skip_next = True
            continue
        filtered_tail.append(arg)
    return [base[0], "exec", "resume", resume_session_id, *filtered_tail]


# ---------------------------------------------------------------------------
# Public argv builders
# ---------------------------------------------------------------------------


def build_argv(
    agent_type: AgentType,
    prompt: str,
    *,
    binary: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    extra_flags: tuple[str, ...] = (),
    context_path: str | None = None,
    project_dir: str | None = None,
    prompt_on_stdin: bool = False,
    prompt_file: str | None = None,
    model_tier: str | None = None,
) -> list[str]:
    """Return the argv list for invoking *agent_type* with *prompt*.

    Each normal dispatch spawns a fresh CLI session — see
    `feedback_persistent_sessions` memory: *general* ``--resume`` was buggy in
    production (silent state-rot late in long sessions) and is not used here.
    The one sanctioned exception is the narrow single-shot JSON-retry re-entry
    (:func:`build_resume_argv` / desktop-dy2j), which resumes the immediately
    prior session exactly once to recover an omitted result block.

    When *prompt_on_stdin* is set the prompt is delivered over the child's
    stdin instead of as an argv element — on Windows the agent CLIs resolve to
    npm ``.cmd`` shims, which expand a large prompt argument through cmd.exe and
    hit its ~8191-char command-line limit ("The command line is too long.").
    Each CLI is told to read the prompt from stdin: codex via the ``-`` prompt
    placeholder, claude via ``-p`` with no prompt argument.
    Grok is the exception — it has no stdin prompt mode, so the caller writes
    the prompt to a temp file and passes its path as *prompt_file*, which Grok
    reads via ``--prompt-file`` (see ``cli_grok.build_argv`` and issue #160).

    *model_tier* is swink-coding-specific (SuperSwink-Coding#282): only used
    when *model* is a ``provider:model[@endpoint]`` tier_map override rather
    than a plain tier alias, to say which tier's backend is being overridden
    for this dispatch. Ignored by every other agent type.

    Exported so tests can assert command shape without spawning a subprocess.
    """
    from agentshore.agents import cli_antigravity, cli_grok, cli_swink_coding

    extra_flags = _apply_yolo_default(agent_type, tuple(extra_flags))
    builders: dict[AgentType, _ArgvBuilder] = {
        AgentType.CLAUDE_CODE: _build_argv_claude_code,
        AgentType.CODEX: _build_argv_codex,
        AgentType.GROK: cli_grok.build_argv,
        AgentType.ANTIGRAVITY: cli_antigravity.build_argv,
        AgentType.SWINK_CODING: cli_swink_coding.build_argv,
    }
    builder = builders.get(agent_type)
    if builder is None:
        msg = f"build_argv: unsupported CLI agent type {agent_type!r}"
        raise ValueError(msg)
    return builder(
        prompt=prompt,
        binary=binary,
        model=model,
        reasoning_effort=reasoning_effort,
        extra_flags=extra_flags,
        context_path=context_path,
        project_dir=project_dir,
        prompt_on_stdin=prompt_on_stdin,
        prompt_file=prompt_file,
        model_tier=model_tier,
    )


def build_resume_argv(
    agent_type: AgentType,
    prompt: str,
    resume_session_id: str,
    *,
    binary: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    extra_flags: tuple[str, ...] = (),
    project_dir: str | None = None,
    prompt_on_stdin: bool = False,
    prompt_file: str | None = None,
    model_tier: str | None = None,
) -> list[str]:
    """Return argv for a single JSON-retry RESUME dispatch (desktop-dy2j).

    A narrow, single-shot re-entry of the *immediately prior* session to recover
    the structured result block the agent omitted — NOT general long-session
    resume (see ``feedback_persistent_sessions``). Each supported CLI exposes a
    resume-by-id flag, and AgentShore holds a stable session id for each:
    claude (``--resume``), codex (``exec resume``), grok (``-r``), antigravity
    (``--conversation``, id from :func:`cli_antigravity.resolve_conversation_id`).

    *model_tier* is swink-coding-specific (SuperSwink-Coding#282) — see
    :func:`build_argv`. Ignored by every other agent type.

    Exported so tests can assert command shape without spawning a subprocess.
    """
    from agentshore.agents import cli_antigravity, cli_grok, cli_swink_coding

    extra_flags = _apply_yolo_default(agent_type, tuple(extra_flags))
    builders: dict[AgentType, _ResumeArgvBuilder] = {
        AgentType.CLAUDE_CODE: _build_resume_argv_claude_code,
        AgentType.CODEX: _build_resume_argv_codex,
        AgentType.GROK: cli_grok.build_resume_argv,
        AgentType.ANTIGRAVITY: cli_antigravity.build_resume_argv,
        AgentType.SWINK_CODING: cli_swink_coding.build_resume_argv,
    }
    builder = builders.get(agent_type)
    if builder is None:
        msg = f"build_resume_argv: unsupported CLI agent type {agent_type!r}"
        raise ValueError(msg)
    return builder(
        resume_session_id=resume_session_id,
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
