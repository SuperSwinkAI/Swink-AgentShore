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
    """Write the Grok prompt to a temporary --prompt-file.

    The caller must unlink the file in its finally block."""
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
    AgentType.COPILOT: ("--yolo",),
}

_RESUMABLE_AGENT_TYPES: frozenset[AgentType] = frozenset(
    {
        AgentType.CLAUDE_CODE,
        AgentType.CODEX,
        AgentType.GROK,
        AgentType.ANTIGRAVITY,
        AgentType.SWINK_CODING,
        AgentType.COPILOT,
    }
)


_PINNABLE_SESSION_AGENT_TYPES: frozenset[AgentType] = frozenset(
    {AgentType.SWINK_CODING, AgentType.GROK, AgentType.COPILOT}
)


_TOOL_DENIAL_CAPABLE_AGENT_TYPES: frozenset[AgentType] = frozenset(
    {AgentType.SWINK_CODING, AgentType.GROK, AgentType.COPILOT}
)


_RESULT_SCHEMA_CAPABLE_AGENT_TYPES: frozenset[AgentType] = frozenset({AgentType.CLAUDE_CODE})


def with_result_schema(
    agent_type: AgentType, argv: list[str], schema_json: str | None
) -> list[str]:
    """Return *argv* with the final-message schema flag added where supported.

    A no-op for agent types outside :data:`_RESULT_SCHEMA_CAPABLE_AGENT_TYPES`
    and when no schema is requested — those dispatches rely on the prompt's
    result contract and the text parser, exactly as before. Also a no-op on
    Windows: the agent CLIs are npm ``.cmd`` shims there, and an inline JSON
    argument does not survive cmd.exe quoting.
    """
    import sys

    if (
        not schema_json
        or agent_type not in _RESULT_SCHEMA_CAPABLE_AGENT_TYPES
        or sys.platform == "win32"
    ):
        return argv
    try:
        anchor = argv.index("stream-json") + 1
    except ValueError:
        return argv
    return [*argv[:anchor], "--json-schema", schema_json, *argv[anchor:]]


def _apply_yolo_default(agent_type: AgentType, extra_flags: tuple[str, ...]) -> tuple[str, ...]:
    """Return YOLO defaults for *agent_type* when the user provided no flags."""
    if extra_flags:
        return extra_flags
    return _DEFAULT_YOLO_FLAGS.get(agent_type, ())


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
        session_id: str | None,
        disallowed_tools: tuple[str, ...],
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
        session_id: str | None,
        disallowed_tools: tuple[str, ...],
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
    session_id: str | None,
    disallowed_tools: tuple[str, ...],
) -> list[str]:
    """Claude Code argv. ``project_dir``, ``prompt_file``, ``model_tier``,
    ``session_id``, and ``disallowed_tools`` are accepted only for
    ``_ArgvBuilder`` signature parity and ignored: Claude has no
    working-directory flag, no prompt-file mode, no tier_map concept, no
    caller-pinned session id, and no per-dispatch tool deny flag.
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
    session_id: str | None,
    disallowed_tools: tuple[str, ...],
) -> list[str]:
    """Codex argv. ``context_path``, ``prompt_file``, ``model_tier``,
    ``session_id``, and ``disallowed_tools`` are accepted only for
    ``_ArgvBuilder`` signature parity and ignored: Codex has no
    system-prompt-file flag, no prompt-file mode, no tier_map concept, no
    caller-pinned session id, and no per-dispatch tool deny flag.
    """
    binary = binary or "codex"
    yolo = "--dangerously-bypass-approvals-and-sandbox" in extra_flags
    args = [binary, "exec", "--json"]
    if not yolo:
        args += ["-c", 'sandbox_mode="workspace-write"']
    if model:
        args += ["-m", model]
    if reasoning_effort:
        args += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
    args += ["-c", "shell_environment_policy.inherit=all"]
    args.extend(extra_flags)
    if project_dir:
        args += ["-C", project_dir]
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
    session_id: str | None,
    disallowed_tools: tuple[str, ...],
) -> list[str]:
    """Claude Code resume argv. ``model``, ``reasoning_effort``, ``project_dir``,
    ``prompt_file``, ``model_tier``, ``session_id``, and ``disallowed_tools``
    are accepted only for ``_ResumeArgvBuilder`` signature parity and ignored:
    ``--resume`` re-enters the prior session verbatim. ``extra_flags`` IS
    forwarded — permission mode is per-invocation, not restored by
    ``--resume``, so dropping the YOLO ``--dangerously-skip-permissions``
    default would run the retry in ``auto`` permission mode.
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
        *extra_flags,
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
    session_id: str | None,
    disallowed_tools: tuple[str, ...],
) -> list[str]:
    """Build Codex resume argv by inserting exec resume <id> into the base argv."""
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
        session_id=None,
        disallowed_tools=disallowed_tools,
    )
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
    session_id: str | None = None,
    disallowed_tools: tuple[str, ...] = (),
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
    Copilot reads piped stdin when ``-p`` is omitted.

    *model_tier* is swink-coding-specific (SuperSwink-Coding#282): only used
    when *model* is a ``provider:model[@endpoint]`` tier_map override rather
    than a plain tier alias, to say which tier's backend is being overridden
    for this dispatch. Ignored by every other agent type.

    *session_id* pins the new run's durable session id so the caller knows it
    before spawn (swink-coding ``--session-id``, SuperSwink-Coding#300; grok
    and copilot ``--session-id <uuid>``). Honoured only by
    :data:`_PINNABLE_SESSION_AGENT_TYPES`; ignored by every other agent type.

    *disallowed_tools* is the executing play's tool-denial policy
    (``Play.disallowed_tools``) — tool names the play must not be able to reach,
    denied at the CLI's own permission layer rather than merely discouraged in
    the prompt. Honoured by the agent types in
    :data:`_TOOL_DENIAL_CAPABLE_AGENT_TYPES` and ignored by the rest, which is
    a capability gap, not a policy exemption: a play's declared denials are
    advisory on any CLI that cannot express them.

    Exported so tests can assert command shape without spawning a subprocess.
    """
    from agentshore.agents import cli_antigravity, cli_copilot, cli_grok, cli_swink_coding

    extra_flags = _apply_yolo_default(agent_type, tuple(extra_flags))
    builders: dict[AgentType, _ArgvBuilder] = {
        AgentType.CLAUDE_CODE: _build_argv_claude_code,
        AgentType.CODEX: _build_argv_codex,
        AgentType.GROK: cli_grok.build_argv,
        AgentType.ANTIGRAVITY: cli_antigravity.build_argv,
        AgentType.SWINK_CODING: cli_swink_coding.build_argv,
        AgentType.COPILOT: cli_copilot.build_argv,
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
        session_id=session_id,
        disallowed_tools=disallowed_tools,
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
    disallowed_tools: tuple[str, ...] = (),
) -> list[str]:
    """Return argv for a single JSON-retry RESUME dispatch (desktop-dy2j).

    A narrow, single-shot re-entry of the *immediately prior* session to recover
    the structured result block the agent omitted — NOT general long-session
    resume (see ``feedback_persistent_sessions``). Each supported CLI exposes a
    resume-by-id flag, and AgentShore holds a stable session id for each:
    claude (``--resume``), codex (``exec resume``), grok (``-r``), antigravity
    (``--conversation``, id from :func:`cli_antigravity.resolve_conversation_id`),
    copilot (``--resume=<id>``).

    *model_tier* is swink-coding-specific (SuperSwink-Coding#282) — see
    :func:`build_argv`. Ignored by every other agent type.

    *disallowed_tools* IS forwarded here, unlike *session_id* below: the retry
    re-enters the same play's session to finish the same unit of work, so the
    play's denials must hold across it. A resume that quietly restored a tool
    the play had denied would make the policy trivially escapable — omit the
    result block once and the deny is gone.

    Exported so tests can assert command shape without spawning a subprocess.
    """
    from agentshore.agents import cli_antigravity, cli_copilot, cli_grok, cli_swink_coding

    extra_flags = _apply_yolo_default(agent_type, tuple(extra_flags))
    builders: dict[AgentType, _ResumeArgvBuilder] = {
        AgentType.CLAUDE_CODE: _build_resume_argv_claude_code,
        AgentType.CODEX: _build_resume_argv_codex,
        AgentType.GROK: cli_grok.build_resume_argv,
        AgentType.ANTIGRAVITY: cli_antigravity.build_resume_argv,
        AgentType.SWINK_CODING: cli_swink_coding.build_resume_argv,
        AgentType.COPILOT: cli_copilot.build_resume_argv,
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
        session_id=None,
        disallowed_tools=disallowed_tools,
    )
