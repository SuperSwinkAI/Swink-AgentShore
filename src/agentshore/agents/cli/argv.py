"""Argv construction helpers for the CLI agent adapter.

Builds the subprocess argument list for each supported CLI agent type. Extracted
from ``cli_agent`` to give argv logic a focused home while keeping ``cli_agent``
as the driver.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

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
}

# Agent types whose CLI exposes a resume-by-id flag AND for which AgentShore
# holds a stable session id (claude/codex/grok parse it from stdout; agy
# resolves it from its on-disk conversation cache). The narrow JSON-retry path
# (desktop-dy2j) re-enters that one session to recover the omitted result block.
_RESUMABLE_AGENT_TYPES: frozenset[AgentType] = frozenset(
    {
        AgentType.CLAUDE_CODE,
        AgentType.CODEX,
        AgentType.GROK,
        AgentType.ANTIGRAVITY,
    }
)


def _apply_yolo_default(agent_type: AgentType, extra_flags: tuple[str, ...]) -> tuple[str, ...]:
    """Return YOLO defaults for *agent_type* when the user provided no flags."""
    if extra_flags:
        return extra_flags
    return _DEFAULT_YOLO_FLAGS.get(agent_type, ())


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

    Exported so tests can assert command shape without spawning a subprocess.
    """
    from agentshore.agents import cli_antigravity, cli_grok

    extra_flags = _apply_yolo_default(agent_type, tuple(extra_flags))
    if agent_type == AgentType.CLAUDE_CODE:
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

    if agent_type == AgentType.CODEX:
        binary = binary or "codex"
        yolo = "--dangerously-bypass-approvals-and-sandbox" in extra_flags
        args = [binary, "exec", "--json"]
        if not yolo:
            args.append("--full-auto")
        if model:
            args += ["-m", model]
        if reasoning_effort:
            args += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        # desktop-pxg: without this, codex's shell tool runs subprocesses (gh,
        # git, etc.) with a stripped env, so the GH_TOKEN we inject for the
        # codex process never reaches `gh api user`. Result: identity mismatch
        # and refused mutations. Setting inherit=all passes our env (including
        # the per-identity GH_TOKEN/GH_CONFIG_DIR) through to every codex
        # shell-tool invocation.
        args += ["-c", "shell_environment_policy.inherit=all"]
        args.extend(extra_flags)
        if project_dir:
            args += ["-C", project_dir]
        # "-" tells `codex exec` to read the prompt from stdin.
        args.append("-" if prompt_on_stdin else prompt)
        return args

    if agent_type == AgentType.GROK:
        return cli_grok.build_argv(
            prompt=prompt,
            binary=binary,
            model=model,
            reasoning_effort=reasoning_effort,
            extra_flags=extra_flags,
            project_dir=project_dir,
            prompt_on_stdin=prompt_on_stdin,
            prompt_file=prompt_file,
        )

    if agent_type == AgentType.ANTIGRAVITY:
        return cli_antigravity.build_argv(
            prompt=prompt,
            binary=binary,
            model=model,
            reasoning_effort=reasoning_effort,
            extra_flags=extra_flags,
            project_dir=project_dir,
            prompt_on_stdin=prompt_on_stdin,
            prompt_file=prompt_file,
        )

    msg = f"build_argv: unsupported CLI agent type {agent_type!r}"
    raise ValueError(msg)


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
) -> list[str]:
    """Return argv for a single JSON-retry RESUME dispatch (desktop-dy2j).

    A narrow, single-shot re-entry of the *immediately prior* session to recover
    the structured result block the agent omitted — NOT general long-session
    resume (see ``feedback_persistent_sessions``). Each supported CLI exposes a
    resume-by-id flag, and AgentShore holds a stable session id for each:
    claude (``--resume``), codex (``exec resume``), grok (``-r``), antigravity
    (``--conversation``, id from :func:`cli_antigravity.resolve_conversation_id`).

    Exported so tests can assert command shape without spawning a subprocess.
    """
    from agentshore.agents import cli_antigravity, cli_grok

    extra_flags = _apply_yolo_default(agent_type, tuple(extra_flags))

    if agent_type == AgentType.CLAUDE_CODE:
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

    if agent_type == AgentType.CODEX:
        base = build_argv(
            agent_type,
            prompt,
            binary=binary,
            model=model,
            reasoning_effort=reasoning_effort,
            extra_flags=extra_flags,
            project_dir=project_dir,
            prompt_on_stdin=prompt_on_stdin,
            prompt_file=prompt_file,
        )
        # base == [binary, "exec", "--json", ...]; turn it into the resume
        # subcommand: [binary, "exec", "resume", <id>, "--json", ...].
        return [base[0], "exec", "resume", resume_session_id, *base[2:]]

    if agent_type == AgentType.GROK:
        return cli_grok.build_resume_argv(
            resume_session_id=resume_session_id,
            prompt=prompt,
            binary=binary,
            model=model,
            reasoning_effort=reasoning_effort,
            extra_flags=extra_flags,
            project_dir=project_dir,
            prompt_on_stdin=prompt_on_stdin,
            prompt_file=prompt_file,
        )

    if agent_type == AgentType.ANTIGRAVITY:
        return cli_antigravity.build_resume_argv(
            resume_session_id=resume_session_id,
            prompt=prompt,
            binary=binary,
            model=model,
            reasoning_effort=reasoning_effort,
            extra_flags=extra_flags,
            project_dir=project_dir,
            prompt_on_stdin=prompt_on_stdin,
            prompt_file=prompt_file,
        )

    msg = f"build_resume_argv: unsupported CLI agent type {agent_type!r}"
    raise ValueError(msg)
