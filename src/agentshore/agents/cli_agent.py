"""CLI agent adapter — asyncio subprocess dispatch for supported CLI agents."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, NoReturn, Protocol

from agentshore import subprocess_env
from agentshore.agents import cli_antigravity, cli_grok
from agentshore.agents._jsonl import (
    _iter_json_events,
    _max_usage,
    _usage_totals_from_dict,
    _UsageTotals,
)
from agentshore.agents.costs import estimate_cost
from agentshore.agents.handle import AgentInvocationResult
from agentshore.agents.pricing import PricingQuote, default_quote
from agentshore.errors import (
    AgentOutputInvalid,
    AgentProcessCrashed,
    AgentProcessError,
    ErrorClass,
    PlayTimeoutError,
)
from agentshore.logging import get_logger
from agentshore.state import AgentType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.agents.handle import AgentHandle
    from agentshore.config import AgentConfig

_logger = get_logger(__name__)


_DEFAULT_TIMEOUT = 10800  # seconds (3h) — max-runtime wall-clock backstop when
# neither AgentConfig.timeout nor a resolved default_timeout is supplied. The
# primary kill is the silence-based stream_idle watchdog (default 1800s); this
# only bounds an agent that streams output for the full duration without
# finishing. See AgentManager.dispatch / RuntimeConfig.agent_timeout.
_SIGKILL_GRACE = 10  # seconds between SIGTERM and SIGKILL
_LINE_DRIFT_WARN_BYTES = 1_048_576  # warn once if any single line exceeds 1MB
_ARGV_PREVIEW_MAX_CHARS = 256  # log clamp; full prompt is reconstructible from skill+params


# ---------------------------------------------------------------------------
# Inline helpers (migrated from agents/cli_process.py, #107)
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


async def _feed_prompt_stdin(proc: asyncio.subprocess.Process, prompt: str) -> None:
    """Write *prompt* to the child's stdin and close it."""
    stdin = proc.stdin
    if stdin is None:
        return
    try:
        stdin.write(prompt.encode("utf-8"))
        await stdin.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            stdin.close()


# YOLO permission flags applied by default per agent type. AgentShore is an
# autonomous orchestrator — agents can't pause for human approval on each
# `gh` call, so we bypass the per-tool permission gates the CLIs ship with.
# The user can opt out by explicitly setting any non-empty extra_flags in
# agentshore.yaml; that signals "I'm managing flags myself."
# Each category carries two pattern sets (#19). The *stderr* set is the full
# list: a CLI's own stderr is pure diagnostics, so matching anything there is
# safe. The *stdout-safe* set is the subset of high-precision phrases that
# effectively never appear in legitimate agent output. For a CLI **coding**
# agent, stdout is the work PRODUCT — code, diffs, tool output, model
# reasoning — and genuine quota/auth signals from `gh`/`git` tools are embedded
# there too (tool_result JSONL), so we cannot ignore stdout entirely. But the
# generic tokens ("429", "403", "forbidden", "timeout", "overloaded",
# "capacity", "model not found", ...) routinely occur in code an agent edits;
# matching those in stdout misclassified ordinary task failures as
# rate_limit/auth, corrupting the RL reward signal, firing spurious take_break
# recovery, and (for auth/invalid_model) tearing down a working agent. So the
# stdout-safe sets drop every generic token and keep only distinctive phrases.
_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "overloaded",
    "capacity",
    "retry after",
    "throttl",
)
_RATE_LIMIT_STDOUT = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "retry after",
    "hit your session limit",
)
_AUTH_PATTERNS = (
    "unauthorized",
    "401",
    "authentication",
    "invalid api key",
    "bad credentials",
    "forbidden",
    "403",
    "irrecoverable github access failure",
    "github connector returned 404",
    "connector repo 404",
    "could not resolve to a repository with the name",
    "could not resolve to a repository",
    "repository/pr is not accessible",
    "not found/could not resolve repository",
    "repository is not resolvable to this token",
    "not resolvable to this token/session",
    "lacks access to repository",
    "cannot access repository metadata",
    # Codex backend-session TTL-expiry signatures (#zeke auth-hang): when the
    # ChatGPT-backed session token expires mid-run the Codex CLI prints these to
    # stderr then hangs reading stdin instead of exiting non-zero. stderr-only —
    # deliberately NOT mirrored into _AUTH_STDOUT so they never match an agent's
    # own work product.
    "failed to renew cache ttl",
    "failed to refresh available models",
)
# Drops the short generic tokens ("401"/"403"/"unauthorized"/"forbidden"/
# "authentication") that appear in code; keeps the distinctive gh/git access
# strings an agent echoes from a real `gh` tool failure.
_AUTH_STDOUT = (
    "invalid api key",
    "bad credentials",
    "irrecoverable github access failure",
    "github connector returned 404",
    "connector repo 404",
    "could not resolve to a repository with the name",
    "could not resolve to a repository",
    "repository/pr is not accessible",
    "not found/could not resolve repository",
    "repository is not resolvable to this token",
    "not resolvable to this token/session",
    "lacks access to repository",
    "cannot access repository metadata",
)
# #190: cache-renewal markers within _AUTH_PATTERNS that the Codex CLI also
# prints during a *transient* model-cache TTL blip (not a real auth rejection).
# When one of these coincides with an EOF-parse marker (below) on the same
# stderr tail it is the transient EOF-renewal shape, NOT a session-token expiry,
# and must NOT abort an in-flight dispatch (observed 415s of work killed). A
# bare cache-renewal line with no parse-EOF suffix is still a genuine token
# expiry and keeps tripping via _AUTH_PATTERNS.
_CACHE_RENEWAL_MARKERS = ("failed to renew cache ttl", "failed to refresh available models")
_PARSE_EOF_MARKERS = ("eof while parsing", "parsing a value")


def _is_transient_cache_blip(lowered: str) -> bool:
    """#190: True iff the stderr tail is the transient cache-renewal EOF-parse blip.

    Suppresses an auth abort only for the cache-renewal-EOF shape (e.g.
    ``failed to renew cache TTL: EOF while parsing a value at line 1 column 0``).
    A real backend-auth rejection (401/403/unauthorized/invalid api key/etc.)
    is unaffected: those markers carry no cache-renewal marker, so this returns
    False and the auth hit trips normally — even if a 401 happens to coexist
    with a cache-renewal line, the presence of the genuine auth marker is what
    keeps ``feed`` matching while this guard only inspects the renewal+EOF pair.
    """
    return any(m in lowered for m in _CACHE_RENEWAL_MARKERS) and any(
        m in lowered for m in _PARSE_EOF_MARKERS
    )


_TIMEOUT_PATTERNS = ("timeout", "timed out", "deadline exceeded", "context deadline")
# All timeout tokens are common in source code/test names — none are safe to
# match against the work product.
_TIMEOUT_STDOUT: tuple[str, ...] = ()
_INVALID_MODEL_PATTERNS = (
    "modelnotfounderror",
    "model not found",
    "requested entity was not found",
    "not found for api version",
    "not found or is not supported",
    "not supported when using codex with a chatgpt account",
    "invalid_request_error",
)
# Keep only the distinctive Codex CLI phrasings it prints to stdout; drop the
# generic "model not found"/"requested entity..."/"invalid_request_error" that
# can appear in code an agent writes (invalid_model triggers agent teardown).
_INVALID_MODEL_STDOUT = (
    "not found or is not supported",
    "not supported when using codex with a chatgpt account",
)
# Codex CLI internal error: its rollout-recording layer references a session
# thread id it can't find on disk. desktop-yxlj observed one occurrence in
# 4600+ plays. The error is permanent for the current codex process but a
# fresh `codex exec` lands a new thread id, so spawning again recovers.
# Pulling this out of the "unknown" bucket gives operators a queryable signal
# for recurrence rate and lets the existing take_break recovery path fire
# under a typed name instead of the generic catch-all.
_CODEX_ROLLOUT_PATTERNS = ("failed to record rollout items",)
# Out-of-memory signatures. An OS OOM kill usually arrives as SIGKILL (rc -9)
# with little/no agent output, but some runtimes log a signature too. Matching
# either routes the exit to ``crash_oom`` (#7) so it is NOT treated as a
# rate-limit.
_OOM_PATTERNS = (
    "out of memory",
    "oomkilled",
    "enomem",
    "cannot allocate memory",
    "memory exhausted",
)
# Host disk exhaustion surfaced by the agent subprocess (a build writing into a
# full worktree, git, npm, cargo, etc.). An environment condition, not the
# agent's fault — pulling it out of "unknown" lets operators see the real cause
# instead of blaming a code/test failure (#180). Distinctive enough to match in
# either stream.
_ENOSPC_PATTERNS = (
    "no space left on device",
    "enospc",
    "errno 28",
    "disk quota exceeded",
)
# Transient network/socket failures. claude_code has been observed to exit with
# "API Error: The socket connection was closed unexpectedly" falling into the
# generic "unknown" bucket (#23). These are distinctive enough to match
# in either stream and are genuinely transient (a retry/take_break recovers), so
# pulling them out of "unknown" gives operators an accurate signal instead of a
# catch-all while keeping the same recovery treatment.
_TRANSIENT_NETWORK_PATTERNS = (
    "socket connection was closed unexpectedly",
    "connection reset by peer",
    "econnreset",
    "socket hang up",
)


def _classify_error(rc: int, stderr: str, stdout: str) -> ErrorClass:
    """Classify a non-zero CLI exit into a semantic error bucket.

    Returns one of ``ErrorClass.RATE_LIMIT``, ``ErrorClass.AUTH``,
    ``ErrorClass.TIMEOUT``, ``ErrorClass.INVALID_MODEL``,
    ``ErrorClass.CODEX_ROLLOUT``, ``ErrorClass.TRANSIENT_NETWORK``,
    ``ErrorClass.CRASH_OOM``, ``ErrorClass.CRASH_SIGNAL``, or
    ``ErrorClass.UNKNOWN`` (each a ``str`` subclass, so callers comparing
    against the bare strings keep working).

    *stderr* is matched against the full pattern set; the trailing 1 000 chars
    of *stdout* are matched only against each category's high-precision
    stdout-safe subset (#19). stdout is inspected at all because some CLIs
    (notably Claude Code) report quota exhaustion on stdout with nothing on
    stderr, and `gh`/`git` tool failures surface in the agent's stdout JSONL —
    but stdout is also the coding agent's work product, so matching generic
    tokens there misclassified ordinary task failures (e.g. a failed file edit)
    as rate_limit/auth. ``rc`` is inspected for signal deaths last, so an
    explicit content message still wins over the raw return code.
    """
    err = stderr.lower()
    out = stdout[-1000:].lower()

    def hit(stderr_patterns: tuple[str, ...], stdout_patterns: tuple[str, ...]) -> bool:
        return any(p in err for p in stderr_patterns) or any(p in out for p in stdout_patterns)

    if hit(_RATE_LIMIT_PATTERNS, _RATE_LIMIT_STDOUT):
        return ErrorClass.RATE_LIMIT
    if hit(_AUTH_PATTERNS, _AUTH_STDOUT):
        return ErrorClass.AUTH
    if hit(_TIMEOUT_PATTERNS, _TIMEOUT_STDOUT):
        return ErrorClass.TIMEOUT
    if hit(_INVALID_MODEL_PATTERNS, _INVALID_MODEL_STDOUT):
        return ErrorClass.INVALID_MODEL
    # codex_rollout + OOM signatures are distinctive enough to match in either
    # stream (an OOM "Out of memory" notice legitimately lands on stdout).
    combined = err + out
    if any(p in combined for p in _CODEX_ROLLOUT_PATTERNS):
        return ErrorClass.CODEX_ROLLOUT
    if any(p in combined for p in _TRANSIENT_NETWORK_PATTERNS):
        return ErrorClass.TRANSIENT_NETWORK
    if any(p in combined for p in _ENOSPC_PATTERNS):
        return ErrorClass.CRASH_ENOSPC
    if any(p in combined for p in _OOM_PATTERNS):
        return ErrorClass.CRASH_OOM
    # Negative return codes are POSIX signal deaths. SIGKILL (-9) from the OS
    # OOM killer or an external kill is a crash, NOT a rate limit — bucketing it
    # as "unknown" routed it into rate-limit take_break recovery (#7). SIGTERM
    # (-15) and SIGINT (-2) are graceful AgentShore/OS-initiated stops and keep
    # falling through to "unknown".
    if rc < 0 and rc not in (-2, -15):
        return ErrorClass.CRASH_SIGNAL
    return ErrorClass.UNKNOWN


def _process_error_detail(
    *,
    agent_type: AgentType,
    model: str | None,
    error_class: ErrorClass,
    stderr: str,
    stdout: str,
) -> str:
    """Return a concise user-facing subprocess error detail."""
    if error_class == ErrorClass.INVALID_MODEL:
        model_text = f" model {model!r}" if model else ""
        report = _extract_cli_report_path(stderr)
        suffix = f" Full report: {report}" if report else ""
        return (
            f"{agent_type.value}{model_text} is not available to the CLI/API "
            f"(invalid or unsupported model). "
            f"Check agents.{agent_type.value}.model_tiers in agentshore.yaml.{suffix}"
        )

    cleaned = _clean_stderr(stderr)
    if cleaned:
        return cleaned[:500]
    return stdout[-200:] if stdout else "(no output)"


def _extract_cli_report_path(stderr: str) -> str | None:
    marker = "Full report available at:"
    if marker not in stderr:
        return None
    tail = stderr.split(marker, 1)[1].strip()
    return tail.split(None, 1)[0] if tail else None


def _clean_stderr(stderr: str) -> str:
    noisy_prefixes = (
        "YOLO mode is enabled.",
        "Ripgrep is not available.",
        "Falling back to GrepTool.",
    )
    lines = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and not line.strip().startswith(noisy_prefixes)
    ]
    return "\n".join(lines)


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


@dataclass(frozen=True, slots=True)
class _ReadOutput:
    raw: str
    usage: _UsageTotals
    session_id: str | None


_POST_RESPONSE_GRACE_S: Final[float] = 60.0

# Launch-to-first-byte deadline (#177). A wedged child (the intermittent codex
# rollout-thread / keychain race) produces *nothing* and otherwise rides the full
# wall-clock ``timeout`` (default 3h, configurable via ``agent_timeout``) before any
# watchdog fires — the existing ``stream_idle_timeout`` (default 1800s) only catches
# first-byte silence at its
# own, much larger, bound. This dedicated deadline caps the blast radius of a
# launch wedge so the orchestrator retries promptly. It only governs the *first*
# byte; once any stdout arrives, ``_watch_stream_idle`` owns silence detection.
# Recoverable like the other timeouts — the orchestrator re-picks.
#
# The bound is deliberately generous (#213). The original tight 120s cap was
# calibrated to a fast-launch assumption that does not hold for reasoning models:
# Grok (CLI 0.2.32) measures 30–70s to first byte with a variance tail past 90s,
# and on heavy ``code_review`` prompts Grok (killed at its old 240s) went silent
# past its window — the model/relay first-token latency, not local startup, dominates and
# is irreducible via flags. The first-byte deadline's job is catching a *broken*
# child that emits nothing, not bounding slow-but-healthy first-token latency
# (the 3h wall-clock is the real hang backstop). So all streaming agents now
# share one 600s deadline: it clears the measured first-token distribution with
# wide margin while still turning a genuine launch wedge around in ~10 min rather
# than the full hour. Trade-off (#177): a true codex rollout/keychain wedge now
# rides to 600s before the fast-kill instead of 120s. An explicit per-agent
# ``first_byte_timeout_seconds`` in config still overrides this for tuning.
_FIRST_BYTE_DEADLINE_S: Final[float] = 600.0

# Per-agent-type override of the global first-byte deadline. Streaming agents
# (codex, claude, grok) all use the global 600s above; the only override
# is antigravity, which is structurally non-streaming.
_FIRST_BYTE_DEADLINE_BY_TYPE: dict[AgentType, float] = {
    # agy uses an async task system and emits no stdout until the task completes
    # (#217); first byte = task done, which can take up to ~30 min for a heavy
    # coding task. The watchdog still fires for genuine hangs (process exits
    # before emitting anything). This is a structural carve-out, NOT a slow-model
    # tuning — collapsing it to the global
    # 600s would kill healthy long-running agy tasks.
    AgentType.ANTIGRAVITY: 1800.0,
}


def _resolve_first_byte_deadline(
    agent_type: AgentType,
    cfg: AgentConfig,
    timeout: float,
    per_dispatch_override: float | None = None,
) -> float:
    """Resolve the armed first-byte deadline for one dispatch.

    Precedence: an explicit ``per_dispatch_override`` (a one-off short budget for
    a single call, e.g. the no-JSON resume-retry, #232), then the per-agent
    ``first_byte_timeout_seconds`` config override, then the per-agent-type
    built-in default, then the global ``_FIRST_BYTE_DEADLINE_S``. Always clamped
    to the wall-clock ``timeout`` so it never outlives the dispatch.
    """
    if per_dispatch_override is not None:
        base = float(per_dispatch_override)
    else:
        override = cfg.first_byte_timeout_seconds
        base = (
            float(override)
            if override is not None
            else _FIRST_BYTE_DEADLINE_BY_TYPE.get(agent_type, _FIRST_BYTE_DEADLINE_S)
        )
    return min(base, timeout)


@dataclass(slots=True)
class _StdoutActivity:
    last_stdout_at: float
    received_any: bool = False
    response_complete: bool = False
    # Monotonic dispatch-start reference (set by ``_await_output_or_timeout`` from
    # the pre-spawn ``t_start``) and the monotonic instant the first stdout byte
    # arrived. Together they give an accurate time-to-first-byte for #212. Left at
    # the 0.0 / None defaults when the activity is constructed without a dispatch
    # context (e.g. focused unit tests of ``_read_output``), which suppresses the
    # ``cli_first_byte`` emission below rather than logging a garbage elapsed.
    dispatch_start: float = 0.0
    first_byte_at: float | None = None

    def mark(self) -> bool:
        """Record stdout activity; return True only on the very first byte."""
        now = time.monotonic()
        self.last_stdout_at = now
        first = not self.received_any
        self.received_any = True
        if first:
            self.first_byte_at = now
        return first

    def mark_response_complete(self) -> None:
        self.response_complete = True
        self.last_stdout_at = time.monotonic()


@dataclass(slots=True)
class _StderrSniffer:
    """Accumulates a live dispatch's stderr and flags auth-expiry signatures.

    Two jobs in one drain of ``proc.stderr``:

    1. Capture the stderr text so ``_finalize_nonzero_exit`` can still classify a
       normal non-zero exit (the prior code re-read ``proc.stderr`` at the end;
       once we drain it live for sniffing, that read would come back empty).
    2. Watch a bounded tail for the ``_AUTH_PATTERNS`` markers so a backend
       session-token expiry (Codex prints "failed to renew cache ttl" then hangs
       on stdin, never exiting) is killed in well under a second and classified
       AUTH instead of waiting out the full ``stream_idle_timeout`` and being
       mislabelled TIMEOUT_STREAM_IDLE.
    """

    # Bounded tail inspected for auth markers (case-insensitive). 8 KiB easily
    # covers the multi-line Codex auth banner without scanning unbounded output.
    tail_window: int = 8192
    # Cap on the stderr text retained for _finalize_nonzero_exit classification.
    capture_cap: int = 65536
    tail: str = ""
    captured: str = ""
    auth_hit: bool = False

    def feed(self, text: str) -> bool:
        """Append decoded stderr; return ``True`` on the first auth match."""
        if len(self.captured) < self.capture_cap:
            self.captured = (self.captured + text)[: self.capture_cap]
        self.tail = (self.tail + text)[-self.tail_window :]
        if not self.auth_hit:
            lowered = self.tail.lower()
            # #190: a genuine auth marker that is NOT one of the cache-renewal
            # markers (e.g. 401/403/unauthorized/invalid api key) always trips,
            # even if a transient cache-renewal+EOF line coexists in the tail.
            hard_auth = any(p in lowered for p in _AUTH_PATTERNS if p not in _CACHE_RENEWAL_MARKERS)
            cache_auth = any(p in lowered for p in _CACHE_RENEWAL_MARKERS)
            # Cache-renewal-only signal is suppressed when it is the transient
            # EOF-parse variant; a bare cache-renewal line still trips.
            if hard_auth or (cache_auth and not _is_transient_cache_blip(lowered)):
                self.auth_hit = True
                return True
        return False


async def _watch_stderr_auth(
    proc: asyncio.subprocess.Process,
    sniffer: _StderrSniffer,
    *,
    agent_id: str,
    agent_type: str,
) -> NoReturn:
    """Drain stderr live; raise ``PlayTimeoutError(AUTH)`` on an auth signature.

    On EOF with no auth hit this sleeps forever (it loses the read/idle race and
    is cancelled by the caller). It never returns a value — either it raises on
    an auth marker or it is cancelled once stdout/idle resolves the dispatch.
    """
    if proc.stderr is not None:
        try:
            async for raw_line in proc.stderr:
                text = raw_line.decode("utf-8", errors="replace")
                if sniffer.feed(text):
                    _logger.warning(
                        "cli_agent_stderr_auth_abort",
                        agent_id=agent_id,
                        agent_type=agent_type,
                        stderr_tail=sniffer.tail[-500:],
                    )
                    raise PlayTimeoutError(
                        f"agent {agent_id!r} ({agent_type}) emitted a backend-auth "
                        f"signature on stderr; aborting dispatch",
                        error_class=ErrorClass.AUTH,
                    )
        except (OSError, EOFError):
            pass
    # stderr drained without an auth hit (or no stderr pipe): yield to the
    # read/idle race, which will cancel this task once the dispatch resolves.
    while True:
        await asyncio.sleep(3600.0)


@dataclass(frozen=True, slots=True)
class _ReadOutputFailed:
    exc: BaseException


@dataclass(frozen=True, slots=True)
class _DispatchArgv:
    """Packaged argv + derived log fields produced by ``_build_dispatch_argv``."""

    argv: list[str]
    prompt_bytes: int
    argv_str: str  # truncated preview for ``cli_dispatch_start`` log event


def _apply_yolo_default(agent_type: AgentType, extra_flags: tuple[str, ...]) -> tuple[str, ...]:
    """Return YOLO defaults for *agent_type* when the user provided no flags."""
    if extra_flags:
        return extra_flags
    return _DEFAULT_YOLO_FLAGS.get(agent_type, ())


# ---------------------------------------------------------------------------
# Public helpers
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


# ---------------------------------------------------------------------------
# Core dispatch helpers
# ---------------------------------------------------------------------------
def _build_dispatch_argv(
    handle: AgentHandle,
    prompt: str,
    *,
    cfg: AgentConfig,
    python_executable: str | None,
    resume_session_id: str | None,
    effective_cwd: Path,
    prompt_file: str | None = None,
) -> _DispatchArgv:
    """Build the subprocess argv list and log-preview fields for a single dispatch.

    Encapsulates the test-shim path (``python_executable``), the normal
    ``build_argv`` path, and the narrow JSON-retry ``--resume`` override
    (desktop-dy2j). *prompt_file*, when set, routes a Grok dispatch's prompt
    through ``--prompt-file`` instead of an argv element (issue #160).
    """
    prompt_on_stdin = _prompt_on_stdin(python_executable)
    if python_executable is not None:
        # Test shim: invoke cfg.binary as a Python script.
        argv: list[str] = [python_executable, cfg.binary or ""]
        # A resuming shim dispatch keeps the minimal claude-style --resume shape
        # (the shim is a python mock, not a real CLI — only the flag's presence
        # matters to the tests that exercise this path).
        if resume_session_id is not None and handle.agent_type == AgentType.CLAUDE_CODE:
            argv = [
                argv[0],
                "--resume",
                resume_session_id,
                "-p",
                "--verbose",
                "--output-format",
                "stream-json",
            ]
            if not prompt_on_stdin:
                argv.append(prompt)
    elif resume_session_id is not None and handle.agent_type in _RESUMABLE_AGENT_TYPES:
        # desktop-dy2j: narrow JSON-retry re-entry of the prior session so the
        # agent emits the structured trailer it missed. Per-agent resume shape.
        argv = build_resume_argv(
            handle.agent_type,
            prompt,
            resume_session_id,
            binary=cfg.binary,
            model=handle.model or cfg.model,
            reasoning_effort=handle.reasoning_effort or cfg.reasoning_effort,
            extra_flags=cfg.extra_flags,
            project_dir=str(effective_cwd),
            prompt_on_stdin=prompt_on_stdin,
            prompt_file=prompt_file,
        )
    else:
        argv = build_argv(
            handle.agent_type,
            prompt,
            binary=cfg.binary,
            model=handle.model or cfg.model,
            reasoning_effort=handle.reasoning_effort or cfg.reasoning_effort,
            extra_flags=cfg.extra_flags,
            project_dir=str(effective_cwd),
            prompt_on_stdin=prompt_on_stdin,
            prompt_file=prompt_file,
        )

    prompt_bytes = len(prompt.encode("utf-8"))

    # Clamp argv_preview to keep the log readable. The last argv element is the
    # full skill prompt (~7 KB), and embedding it in every dispatch event bloats
    # the orchestrator log by ~1 MB per session. The full prompt is
    # reconstructible from the skill template plus the PlayParams that hit the
    # dispatcher, so keep only the leading flags here.
    argv_str = " ".join(argv[:10])
    if len(argv_str) > _ARGV_PREVIEW_MAX_CHARS:
        truncated = len(argv_str) - _ARGV_PREVIEW_MAX_CHARS
        argv_str = argv_str[:_ARGV_PREVIEW_MAX_CHARS] + f"…(+{truncated} chars truncated)"

    return _DispatchArgv(argv=argv, prompt_bytes=prompt_bytes, argv_str=argv_str)


async def _await_output_or_timeout(
    proc: asyncio.subprocess.Process,
    handle: AgentHandle,
    *,
    max_bytes: int,
    cfg: AgentConfig,
    stream_idle_timeout: float,
    timeout: float,
    prompt_bytes: int,
    sniffer: _StderrSniffer,
    dispatch_start: float,
    first_byte_timeout_override: float | None = None,
) -> tuple[_ReadOutput, bool]:
    """Drive the read/idle/stderr-auth race and return ``(result, post_response_killed)``.

    Called inside ``dispatch_cli``'s outer ``try:`` block so that the caller's
    ``except``/``finally`` clauses remain responsible for process cleanup on
    error.  Raises ``PlayTimeoutError`` directly on wall-clock or idle expiry;
    re-raises ``_ReadOutputFailed.exc`` on output-parse errors. The
    ``stderr``-auth watcher raises ``PlayTimeoutError(error_class=AUTH)`` as soon
    as a backend session-token expiry signature lands on stderr, so a stdin-hang
    is killed in well under a second rather than after the full idle window.
    """
    post_response_killed = False
    stdout_activity = _StdoutActivity(
        last_stdout_at=time.monotonic(), dispatch_start=dispatch_start
    )
    read_task = asyncio.create_task(
        _read_output_guarded(
            proc,
            handle.agent_type,
            max_bytes,
            line_limit=cfg.line_limit_bytes,
            agent_id=handle.agent_id,
            stdout_activity=stdout_activity,
        )
    )
    idle_task = asyncio.create_task(
        _watch_stream_idle(
            stdout_activity,
            timeout=stream_idle_timeout,
            agent_id=handle.agent_id,
            agent_type=handle.agent_type.value,
            model_tier=handle.model_tier,
            prompt_bytes=prompt_bytes,
        )
    )
    auth_task = asyncio.create_task(
        _watch_stderr_auth(
            proc,
            sniffer,
            agent_id=handle.agent_id,
            agent_type=handle.agent_type.value,
        )
    )
    # Launch-to-first-byte watchdog (#177). Caps a silent-launch wedge at
    # ``_FIRST_BYTE_DEADLINE_S`` (~2 min) instead of the full wall-clock
    # ``timeout``. Clamped to ``timeout`` so it never outlives the dispatch.
    #
    # Both watchdogs below are armed unconditionally for EVERY dispatch — there
    # is no play-type or agent-type branching on this path, so ``cleanup`` (a
    # SkillBackedPlay, same path as ``issue_pickup``) is covered identically.
    #
    # Residual gap (#177): these watchdogs only see *byte arrival* via
    # ``_StdoutActivity`` (last_stdout_at / received_any). Model-API-call /
    # turn-count usage is parsed only AFTER the read loop drains (see
    # ``_read_output`` → ``parser.parse``), so a child that streams stdout NOISE
    # while making ZERO model calls keeps ``received_any`` True and ``last_stdout_at``
    # fresh, defeating both byte-watchdogs. No live call counter is exposed at
    # this layer; closing this residual needs a higher-layer (per-call/cost)
    # signal — do NOT plumb a new counter here.
    first_byte_task = asyncio.create_task(
        _watch_first_byte(
            stdout_activity,
            deadline=_resolve_first_byte_deadline(
                handle.agent_type, cfg, timeout, first_byte_timeout_override
            ),
            agent_id=handle.agent_id,
            agent_type=handle.agent_type.value,
            model_tier=handle.model_tier,
            prompt_bytes=prompt_bytes,
        )
    )
    # All non-read watchers, for uniform cancellation on every exit path.
    watcher_tasks = (idle_task, auth_task, first_byte_task)

    done, _pending = await asyncio.wait(
        {read_task, *watcher_tasks},
        timeout=float(timeout),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if not done:
        read_task.cancel()
        for watcher in watcher_tasks:
            watcher.cancel()
        await asyncio.gather(read_task, *watcher_tasks, return_exceptions=True)
        # desktop-awc: enrich the wall-clock timeout error with the
        # agent shape so operators can spot whether timeouts correlate
        # with model tier or huge prompts without having to rejoin the
        # play_id back to the agents table by hand.
        raise PlayTimeoutError(
            (
                f"agent {handle.agent_id!r} ({handle.agent_type.value}/"
                f"{handle.model_tier or '?'}) timed out after {timeout}s "
                f"(prompt_bytes={prompt_bytes})"
            ),
            error_class=ErrorClass.TIMEOUT_WALLCLOCK,
        ) from None
    # The stderr-auth watcher fired before stdout/idle resolved: kill the hung
    # process and surface the AUTH classification immediately. The watcher only
    # ever completes by raising ``PlayTimeoutError(AUTH)`` (post-EOF it sleeps
    # indefinitely), so a done auth_task that beat the read always carries an
    # exception to re-raise.
    if auth_task in done and read_task not in done:
        auth_exc = auth_task.exception()
        read_task.cancel()
        for watcher in watcher_tasks:
            if watcher is not auth_task:
                watcher.cancel()
        await asyncio.gather(
            read_task,
            *(w for w in watcher_tasks if w is not auth_task),
            return_exceptions=True,
        )
        raise (
            auth_exc
            if auth_exc is not None
            else AssertionError("stderr-auth watcher completed without raising")
        )
    if read_task in done:
        for watcher in watcher_tasks:
            watcher.cancel()
        await asyncio.gather(*watcher_tasks, return_exceptions=True)
        read_result = await read_task
        if isinstance(read_result, _ReadOutputFailed):
            raise read_result.exc
        return read_result, post_response_killed
    else:
        # A watchdog fired before the read completed. The first-byte watchdog and
        # the idle watcher both raise PlayTimeoutError; the stderr-auth watcher is
        # handled above. Pick whichever of idle/first-byte finished and cancel the
        # rest. The first-byte deadline only fires when no stdout ever arrived, so
        # it shares the stream-idle handling below.
        fired_watcher = first_byte_task if first_byte_task in done else idle_task
        for watcher in watcher_tasks:
            if watcher is not fired_watcher:
                watcher.cancel()
        await asyncio.gather(
            *(w for w in watcher_tasks if w is not fired_watcher),
            return_exceptions=True,
        )
        idle_exc = fired_watcher.exception()
        if idle_exc is None:
            read_result = await read_task
        elif (
            isinstance(idle_exc, PlayTimeoutError)
            and getattr(idle_exc, "error_class", None) == ErrorClass.TIMEOUT_POST_RESPONSE
        ):
            post_response_killed = True
            _logger.info(
                "post_response_process_kill",
                agent_id=handle.agent_id,
                grace_s=_POST_RESPONSE_GRACE_S,
            )
            await _kill_process(proc, handle.agent_id)
            try:
                read_result = await asyncio.wait_for(read_task, timeout=5.0)
            except TimeoutError:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
                raise idle_exc from None
        else:
            grace_s = min(stream_idle_timeout, 0.25)
            try:
                await asyncio.wait_for(asyncio.shield(read_task), timeout=grace_s)
            except TimeoutError:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
                raise idle_exc from None
            read_result = await read_task

        if isinstance(read_result, _ReadOutputFailed):
            raise read_result.exc
        return read_result, post_response_killed


async def _finalize_nonzero_exit(
    proc: asyncio.subprocess.Process,
    handle: AgentHandle,
    *,
    cfg: AgentConfig,
    rc: int,
    raw_output: str,
    sniffer: _StderrSniffer | None = None,
) -> NoReturn:
    """Read stderr, classify the error, log, and raise ``AgentProcessError``.

    Always raises — the ``-> NoReturn`` annotation lets mypy verify callers
    need not handle a return value.

    The live stderr sniffer has already drained ``proc.stderr`` for this
    dispatch, so prefer its captured text; only fall back to re-reading the pipe
    when no sniffer ran (defensive — keeps the classifier working if the live
    drain was ever skipped).
    """
    stderr_text = sniffer.captured if sniffer is not None else ""
    if not stderr_text and proc.stderr:
        try:
            raw_err = await proc.stderr.read()
            stderr_text = raw_err.decode("utf-8", errors="replace")
        except (OSError, EOFError) as exc:
            _logger.warning(
                "cli_agent_stderr_read_failed",
                agent_id=handle.agent_id,
                error=str(exc),
            )
    error_class = _classify_error(rc, stderr_text, raw_output)
    handle.last_error_class = error_class
    _logger.warning(
        "cli_agent_nonzero_exit",
        agent_id=handle.agent_id,
        exit_code=rc,
        error_class=error_class,
        stderr_tail=stderr_text[:500],
        stdout_tail=raw_output[-500:] if raw_output else "(empty)",
    )
    detail = _process_error_detail(
        agent_type=handle.agent_type,
        model=handle.model or cfg.model,
        error_class=error_class,
        stderr=stderr_text,
        stdout=raw_output,
    )
    _close_process_transport(proc)
    raise AgentProcessError(
        f"agent {handle.agent_id!r} exited with code {rc} [{error_class}]: {detail}"
    )


# ---------------------------------------------------------------------------
# Core dispatch
# ---------------------------------------------------------------------------


async def dispatch_cli(
    handle: AgentHandle,
    prompt: str,
    *,
    cfg: AgentConfig,
    pricing: PricingQuote | None = None,
    default_timeout: int = _DEFAULT_TIMEOUT,
    python_executable: str | None = None,
    identity_env: dict[str, str] | None = None,
    on_subprocess_spawned: Callable[[int], Awaitable[None]] | None = None,
    on_subprocess_exited: Callable[[int, int | None], Awaitable[None]] | None = None,
    cwd_override: Path | None = None,
    resume_session_id: str | None = None,
    first_byte_timeout_override: float | None = None,
) -> AgentInvocationResult:
    """Invoke the agent CLI and return raw output + metadata.

    Parameters
    ----------
    handle:
        The AgentHandle owning this agent; used to read binary/type/working_dir.
    prompt:
        Pre-rendered skill prompt to pass to the agent.
    cfg:
        Per-agent configuration from ``RuntimeConfig.agents[name]``.
    pricing:
        Resolved per-model :class:`~agentshore.agents.pricing.PricingQuote` used
        to price this dispatch's token usage. ``None`` (direct/test callers)
        falls back to the bundled global-default quote; the manager always
        resolves the live quote from ``RuntimeConfig.pricebook``.
    python_executable:
        If set, ``cfg.binary`` is treated as a Python script path invoked with
        this interpreter.  Used by tests to run ``mock_agent.py`` through the
        production code path.
    identity_env:
        Optional env-var overlay (e.g. ``GIT_AUTHOR_*``, ``GH_TOKEN``) applied
        on top of ``os.environ`` for the spawned subprocess. ``None`` or empty
        preserves the inherit-parent-env behaviour.
    cwd_override:
        When supplied, replaces ``handle.working_dir`` for this single
        dispatch's cwd (and the ``--project-dir`` style flag in ``argv``).
        The handle is not mutated — concurrent dispatches against the same
        handle may each target a different worktree. ``AGENTSHORE_PROJECT_PATH``
        in ``identity_env`` continues to point at the main repo.
    first_byte_timeout_override:
        One-off launch-to-first-byte budget for this single dispatch, overriding
        the per-agent/per-type defaults (still clamped to the wall-clock timeout).
        Set on the no-JSON resume-retry (#232) so a re-emission can't inherit
        agy's 1800s fresh-task deadline and hang. ``None`` = default resolution.

    Each call spawns a fresh CLI session, except the narrow single-shot
    JSON-retry path: when *resume_session_id* is set, the prior session is
    re-entered once to recover an omitted result block (desktop-dy2j). General
    long-session ``--resume`` remains banned — see ``feedback_persistent_sessions``.
    """
    timeout = cfg.timeout if cfg.timeout is not None else default_timeout
    stream_idle_timeout = float(cfg.stream_idle_timeout)
    # Safety clamp (#177): a misconfigured ``stream_idle_timeout`` larger than the
    # wall-clock ``timeout`` would let the silence watchdog never fire before the
    # dispatch is force-killed — effectively disabling early silence detection. Cap
    # it at the dispatch timeout so the idle watcher always gets a chance to run.
    stream_idle_timeout = min(stream_idle_timeout, float(timeout))
    max_bytes = cfg.max_output_size

    effective_cwd = cwd_override if cwd_override is not None else handle.working_dir

    # Grok can't take the prompt over stdin (no stdin mode) and rejects an empty
    # ``-p`` ("--single: prompt is empty", issue #160). Where the other CLIs
    # would route the prompt over stdin (Windows arg-length limits), Grok
    # instead reads it from a temp file via ``--prompt-file``. Cleaned up in the
    # ``finally`` below.
    grok_prompt_file: Path | None = None
    if handle.agent_type == AgentType.GROK and _prompt_on_stdin(python_executable):
        grok_prompt_file = _write_grok_prompt_file(prompt)

    _argv = _build_dispatch_argv(
        handle,
        prompt,
        cfg=cfg,
        python_executable=python_executable,
        resume_session_id=resume_session_id,
        effective_cwd=effective_cwd,
        prompt_file=str(grok_prompt_file) if grok_prompt_file is not None else None,
    )
    argv, prompt_bytes, argv_str = _argv.argv, _argv.prompt_bytes, _argv.argv_str

    _logger.info(
        "cli_dispatch_start",
        agent_id=handle.agent_id,
        agent_type=str(handle.agent_type),
        argv_preview=argv_str,
        extra_flags=list(cfg.extra_flags),
        dispatch_num=handle.dispatches,
        prompt_bytes=prompt_bytes,
        identity=cfg.identity,
        identity_env_keys=sorted(identity_env) if identity_env else [],
    )

    t_start = time.monotonic()

    # Agent subprocesses run ``git rebase``/``git commit``/``git push`` inside
    # skills, and those invocations don't route through ``command.git``'s
    # hardened env. Route the agent's env through the SAME hardened,
    # non-interactive git policy AgentShore uses for its own git: a no-op editor
    # (so a rebase-internal ``git commit -e`` can't fall back to vim and hang,
    # leaking the worktree — #168) AND ``GIT_TERMINAL_PROMPT=0`` / no askpass (so
    # an agent git op that needs credentials fails *fast* instead of blocking on
    # a credential prompt with no TTY and hanging the full wall-clock — #177).
    # When the agent's identity carries a token, inject it as the per-identity
    # HTTPS credential so the agent authenticates AS ITS OWN identity — each
    # agent subprocess carries its own token, so multi-identity fleets stay
    # correctly attributed (codex pushes as its identity, claude_code as its).
    identity = identity_env or {}
    token = identity.get("GH_TOKEN") or identity.get("GITHUB_TOKEN")
    git_overlay = dict(identity)
    if token:
        git_overlay.update(subprocess_env.git_auth_config_overlay(token))
    env = subprocess_env.hardened_env(
        git_overlay,
        for_git=True,
        for_grok=(handle.agent_type == AgentType.GROK),
        for_antigravity=(handle.agent_type == AgentType.ANTIGRAVITY),
    )

    # Resolve npm-shim agent binaries (codex.cmd etc.) to a full path so they
    # spawn on Windows; CreateProcess only finds bare names ending in .exe.
    argv = _resolve_executable(argv)

    # On Windows the prompt is fed over stdin to dodge the cmd.exe command-line
    # limit (see build_argv); elsewhere stdin stays closed. Two exceptions keep
    # stdin closed because they never read the prompt from it: Grok (it's in
    # --prompt-file) and Antigravity (``agy`` has no stdin mode — the prompt is
    # always in ``-p``). Opening a PIPE and writing a prompt the child never
    # drains could block on a full pipe buffer.
    prompt_on_stdin = (
        _prompt_on_stdin(python_executable)
        and grok_prompt_file is None
        and handle.agent_type != AgentType.ANTIGRAVITY
    )

    # Backstop for the worktree-reclaim TOCTOU race (#176): the dispatch cwd is
    # an AgentShore-managed worktree that reconcile / collision-reclaim churn can
    # remove between allocation and spawn. POSIX surfaces a missing cwd as a raw
    # ``FileNotFoundError`` (an ``OSError``) from ``create_subprocess_exec``,
    # which ``manager.dispatch`` re-raises uncaught — the play executor then has
    # no recoverable match and logs ``unexpected_play_error``. Detect it here and
    # raise a *typed* recoverable error instead so the play fails cleanly and PPO
    # re-picks. Done before spawn so the diagnosis is unambiguous (a spawn-time
    # ``FileNotFoundError`` could otherwise mean a missing executable).
    if not os.path.isdir(effective_cwd):
        raise AgentProcessCrashed(f"dispatch cwd (worktree) no longer exists: {effective_cwd}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if prompt_on_stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(effective_cwd),
            limit=cfg.line_limit_bytes,
            start_new_session=True,
            creationflags=subprocess_env.no_window_creationflags(),
            env=env,
        )
    except NotADirectoryError as exc:
        # cwd path exists but is a file, not a directory — same recoverable class
        # as the missing-cwd race above.
        raise AgentProcessCrashed(
            f"dispatch cwd (worktree) is not a directory: {effective_cwd}"
        ) from exc
    except FileNotFoundError as exc:
        # The pre-spawn ``isdir`` check ruled out a missing cwd, so a
        # ``FileNotFoundError`` here is the agent executable being absent. Still
        # recoverable (re-instantiate the agent), so map to the same typed class
        # rather than letting a raw ``OSError`` reach ``unexpected_play_error``.
        raise AgentProcessCrashed(
            f"agent {handle.agent_id!r} executable not found: {argv[0]!r}"
        ) from exc
    handle.process = proc
    # Drains stderr live so a backend-auth signature aborts the dispatch as AUTH
    # (instead of waiting out the idle timeout). It also retains the stderr text
    # for _finalize_nonzero_exit, which can no longer re-read a now-drained pipe.
    stderr_sniffer = _StderrSniffer()
    stdin_feeder: asyncio.Task[None] | None = None
    if prompt_on_stdin and proc.stdin is not None:
        stdin_feeder = asyncio.create_task(_feed_prompt_stdin(proc, prompt))
    if on_subprocess_spawned is not None and proc.pid is not None:
        await on_subprocess_spawned(proc.pid)

    try:
        read_result, post_response_killed = await _await_output_or_timeout(
            proc,
            handle,
            max_bytes=max_bytes,
            cfg=cfg,
            stream_idle_timeout=stream_idle_timeout,
            timeout=float(timeout),
            prompt_bytes=prompt_bytes,
            sniffer=stderr_sniffer,
            dispatch_start=t_start,
            first_byte_timeout_override=first_byte_timeout_override,
        )
        # Retained for the narrow JSON-retry path (desktop-dy2j). General
        # --resume dispatch is still banned (see feedback_persistent_sessions).
        raw_output, usage, _observed_session_id = (
            read_result.raw,
            read_result.usage,
            read_result.session_id,
        )
    except TimeoutError:
        await _kill_process(proc, handle.agent_id)
        _close_streams(proc)
        _close_process_transport(proc)
        raise PlayTimeoutError(
            f"agent {handle.agent_id!r} timed out after {timeout}s",
            error_class=ErrorClass.TIMEOUT_WALLCLOCK,
        ) from None
    except asyncio.CancelledError:
        # Task cancellation — clean up the child process before propagating.
        with contextlib.suppress(Exception):
            await _kill_process(proc, handle.agent_id)
        _close_streams(proc)
        _close_process_transport(proc)
        raise
    except Exception:
        # AgentOutputInvalid and other standard errors — ensure the process is cleaned up.
        with contextlib.suppress(Exception):
            await _kill_process(proc, handle.agent_id)
        _close_streams(proc)
        _close_process_transport(proc)
        raise
    finally:
        if stdin_feeder is not None:
            stdin_feeder.cancel()
            with contextlib.suppress(Exception):
                await stdin_feeder
        if grok_prompt_file is not None:
            grok_prompt_file.unlink(missing_ok=True)
        if on_subprocess_exited is not None and proc.pid is not None:
            with contextlib.suppress(Exception):
                await on_subprocess_exited(proc.pid, proc.returncode)
        handle.process = None

    duration_ms = int((time.monotonic() - t_start) * 1000)

    # agy wraps actual output inside a task-status block; unwrap it so
    # parse_skill_result sees the model's content, not the status envelope.
    if handle.agent_type == AgentType.ANTIGRAVITY:
        raw_output = cli_antigravity.extract_output(raw_output)
        # agy reveals no session id on stdout (no parser), so resolve its
        # conversation id from the on-disk cache keyed by this dispatch's cwd.
        # This gives agy a resumable id for the narrow JSON-retry path
        # (desktop-dy2j), exactly like the parsed agents. Best-effort: None on
        # any miss, which simply means no retry — never fatal.
        if _observed_session_id is None:
            _observed_session_id = cli_antigravity.resolve_conversation_id(
                effective_cwd, home=env.get("HOME")
            )

    rc = proc.returncode
    if rc != 0 and not post_response_killed:
        await _finalize_nonzero_exit(
            proc, handle, cfg=cfg, rc=rc or 1, raw_output=raw_output, sniffer=stderr_sniffer
        )

    if usage.reported_cost > 0:
        # Vendor-authoritative cost (Claude Code's total_cost_usd). See _UsageTotals.
        dollar_cost = usage.reported_cost
        cost_source = "vendor_reported"
    else:
        dollar_cost = estimate_cost(
            usage.tokens_in,
            usage.tokens_out,
            pricing if pricing is not None else default_quote(),
            cached_tokens_in=usage.cached_tokens_in,
            cache_write_tokens_in=usage.cache_write_tokens_in,
        )
        cost_source = "token_derived"
    _logger.info(
        "cli_dispatch_done",
        cost_source=cost_source,
        agent_id=handle.agent_id,
        duration_ms=duration_ms,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        cached_tokens_in=usage.cached_tokens_in,
        cache_write_tokens_in=usage.cache_write_tokens_in,
        turn_count=usage.turn_count,
        max_turn_input_tokens=usage.max_turn_input_tokens,
        dollar_cost=dollar_cost,
        prompt_bytes=prompt_bytes,
        output_length=len(raw_output),
        output_tail=raw_output[-500:] if raw_output else "(empty)",
    )
    _close_process_transport(proc)
    return AgentInvocationResult(
        raw_output=raw_output,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        cached_tokens_in=usage.cached_tokens_in,
        cache_write_tokens_in=usage.cache_write_tokens_in,
        turn_count=usage.turn_count,
        max_turn_input_tokens=usage.max_turn_input_tokens,
        dollar_cost=dollar_cost,
        duration_ms=duration_ms,
        exit_code=rc or 0,
        session_id=_observed_session_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _read_output(
    proc: asyncio.subprocess.Process,
    agent_type: AgentType,
    max_bytes: int,
    *,
    line_limit: int,
    agent_id: str,
    stdout_activity: _StdoutActivity | None = None,
) -> _ReadOutput:
    """Stream stdout, accumulate output, extract token metadata.

    Returns raw text, billable token buckets, lightweight turn metrics, and
    the observed CLI session id.
    Raises ``AgentOutputInvalid`` if the output exceeds *max_bytes* or if a
    single line exceeds *line_limit* (the asyncio readline buffer cap).
    """
    if proc.stdout is None:
        msg = "Subprocess stdout is None after create_subprocess_exec"
        raise RuntimeError(msg)
    chunks: list[bytes] = []
    total_bytes = 0
    drift_warned = False

    try:
        async for line in proc.stdout:
            # First stdout byte for this dispatch (#212). ``mark()`` runs on every
            # line (short-circuit keeps it evaluated) but returns True only on the
            # first; emitting here — at the transition, not at dispatch end — keeps
            # the log event's position faithful to real first-byte time so live
            # monitors can tell "slow to start" from "never started" and observe
            # first-byte-deadline enforcement. ``dispatch_start`` is 0.0 only when no
            # dispatch context was supplied (unit tests), so the guard suppresses a
            # garbage elapsed there.
            if (
                stdout_activity is not None
                and stdout_activity.mark()
                and stdout_activity.dispatch_start
                and stdout_activity.first_byte_at
            ):
                _logger.info(
                    "cli_first_byte",
                    agent_id=agent_id,
                    agent_type=str(agent_type),
                    elapsed_ms=int(
                        (stdout_activity.first_byte_at - stdout_activity.dispatch_start) * 1000
                    ),
                )
            total_bytes += len(line)
            if total_bytes > max_bytes:
                raise AgentOutputInvalid(
                    f"agent output exceeded {max_bytes} bytes (max_output_size)"
                )
            if not drift_warned and len(line) >= _LINE_DRIFT_WARN_BYTES:
                drift_warned = True
                _logger.warning(
                    "cli_agent_large_line",
                    agent_id=agent_id,
                    agent_type=str(agent_type),
                    line_bytes=len(line),
                    line_limit=line_limit,
                )
            chunks.append(line)

            if (
                stdout_activity is not None
                and not stdout_activity.response_complete
                and _is_terminal_event(line, agent_type)
            ):
                stdout_activity.mark_response_complete()
    except asyncio.LimitOverrunError as exc:
        raise AgentOutputInvalid(
            f"agent {agent_id!r} stream-json line exceeded {line_limit} bytes "
            f"(consumed={exc.consumed}); raise agents.<name>.line_limit_bytes "
            f"in agentshore.yaml"
        ) from exc
    except ValueError as exc:
        # StreamReader.readline() catches LimitOverrunError internally and
        # re-raises as a bare ValueError on Python 3.12+. Detect the chunk-
        # overflow signature and surface a structured error; otherwise re-raise.
        msg = str(exc)
        if "chunk" in msg and "limit" in msg:
            raise AgentOutputInvalid(
                f"agent {agent_id!r} stream-json line exceeded {line_limit} bytes; "
                f"raise agents.<name>.line_limit_bytes in agentshore.yaml"
            ) from exc
        raise

    raw = b"".join(chunks).decode("utf-8", errors="replace")

    parser = _PARSERS.get(agent_type)
    if parser is not None:
        raw, usage, session_id = parser.parse(raw)
    else:
        usage = _UsageTotals()
        session_id = None

    await proc.wait()
    return _ReadOutput(raw=raw, usage=usage, session_id=session_id)


async def _read_output_guarded(
    proc: asyncio.subprocess.Process,
    agent_type: AgentType,
    max_bytes: int,
    *,
    line_limit: int,
    agent_id: str,
    stdout_activity: _StdoutActivity,
) -> _ReadOutput | _ReadOutputFailed:
    try:
        return await _read_output(
            proc,
            agent_type,
            max_bytes,
            line_limit=line_limit,
            agent_id=agent_id,
            stdout_activity=stdout_activity,
        )
    except BaseException as exc:
        return _ReadOutputFailed(exc)


async def _watch_stream_idle(
    stdout_activity: _StdoutActivity,
    *,
    timeout: float,
    agent_id: str,
    agent_type: str = "?",
    model_tier: str | None = None,
    prompt_bytes: int | None = None,
) -> NoReturn:
    """Kill the dispatch if the agent goes silent for ``timeout`` seconds.

    ``last_stdout_at`` is initialised at dispatch start, so an agent that
    never produces a single byte still gets killed after ``timeout``. The
    prior implementation guarded the check with ``if not received_any:
    continue``, which meant a fully-silent subprocess would never be
    killed by this watcher — observed 2026-05-28 session 08a948ed when
    calibrate_alignment ran for 20+ minutes emitting zero events while
    holding the trunk lock. The bug fix preserves the grace handling
    for legitimate startup latency (the configured ``stream_idle_timeout``
    default of 1800s gives any agent a full window to produce output)
    while ensuring the timeout is the *upper bound*, not "best effort
    if any output appeared."
    """
    sleep_s = min(5.0, max(timeout / 10.0, 0.01))
    while True:
        await asyncio.sleep(sleep_s)
        idle_for = time.monotonic() - stdout_activity.last_stdout_at
        effective = _POST_RESPONSE_GRACE_S if stdout_activity.response_complete else timeout
        if idle_for >= effective:
            tier_label = model_tier or "?"
            extra = f" ({agent_type}/{tier_label}"
            if prompt_bytes is not None:
                extra += f", prompt_bytes={prompt_bytes}"
            extra += ")"
            if stdout_activity.response_complete:
                raise PlayTimeoutError(
                    f"agent {agent_id!r}{extra} response complete but process "
                    f"did not exit within {_POST_RESPONSE_GRACE_S:g}s grace period",
                    error_class=ErrorClass.TIMEOUT_POST_RESPONSE,
                )
            silence_qualifier = (
                "produced no stdout"
                if stdout_activity.received_any
                else "never produced any stdout"
            )
            raise PlayTimeoutError(
                f"agent {agent_id!r}{extra} {silence_qualifier} for {timeout:g}s",
                error_class=ErrorClass.TIMEOUT_STREAM_IDLE,
            )


async def _watch_first_byte(
    stdout_activity: _StdoutActivity,
    *,
    deadline: float,
    agent_id: str,
    agent_type: str = "?",
    model_tier: str | None = None,
    prompt_bytes: int | None = None,
) -> NoReturn:
    """Kill the dispatch if no stdout byte arrives within ``deadline`` seconds (#177).

    A healthy CLI agent streams its first JSON event within seconds of launch;
    a launch-wedged child (codex rollout-thread / keychain race) emits nothing
    and would otherwise ride the full wall-clock ``timeout``. This watchdog fires
    *only* on first-byte silence — once ``received_any`` flips True it exits and
    leaves silence detection to ``_watch_stream_idle``. Recoverable via the same
    ``ErrorClass.TIMEOUT_STREAM_IDLE`` class the idle watcher uses, so the
    orchestrator retries rather than escalating.
    """
    start = time.monotonic()
    sleep_s = min(2.0, max(deadline / 20.0, 0.01))
    while True:
        await asyncio.sleep(sleep_s)
        if stdout_activity.received_any:
            # First byte arrived — hand off to the idle watcher and stop here by
            # sleeping forever (the caller cancels this task on read completion).
            await asyncio.sleep(_NEVER_S)
        if time.monotonic() - start >= deadline:
            tier_label = model_tier or "?"
            extra = f" ({agent_type}/{tier_label}"
            if prompt_bytes is not None:
                extra += f", prompt_bytes={prompt_bytes}"
            extra += ")"
            raise PlayTimeoutError(
                f"agent {agent_id!r}{extra} never produced first byte within "
                f"{deadline:g}s (launch wedge)",
                error_class=ErrorClass.TIMEOUT_STREAM_IDLE,
            )


# Effectively-infinite sleep used to park the first-byte watchdog once it has
# handed off to the idle watcher; the caller always cancels the task on read
# completion, so this never actually elapses.
_NEVER_S: Final[float] = 365 * 24 * 3600.0


# ---------------------------------------------------------------------------
# Stream event detection
# ---------------------------------------------------------------------------


# The terminal stream event each CLI emits once its response is fully written.
# Detecting it lets the idle watcher apply the short _POST_RESPONSE_GRACE_S
# (60s) instead of waiting the full stream_idle_timeout (default 1800s) for a
# finished-but-unexited subprocess. Previously only Claude was wired up, so a
# finished codex lingered up to 30 min — stacking memory across
# plays toward OOM (#21). Codex emits ``turn.completed``; Claude emits ``type: "result"``.
_TERMINAL_EVENT_TYPES: Final[dict[AgentType, frozenset[str]]] = {
    AgentType.CLAUDE_CODE: frozenset({"result"}),
    AgentType.CODEX: frozenset({"turn.completed"}),
    AgentType.GROK: frozenset({"end"}),
}


def _is_terminal_event(line: bytes, agent_type: AgentType) -> bool:
    """Return True if *line* is *agent_type*'s response-complete stream event.

    This is the final event the CLI emits after completing a response.
    Detecting it lets the idle watcher switch to a short grace period so
    lingering background tasks don't block process exit for 30 minutes (#21).
    """
    terminal_types = _TERMINAL_EVENT_TYPES.get(agent_type)
    if not terminal_types:
        return False
    # Cheap pre-filter: skip json.loads unless a terminal type name appears.
    if not any(t.encode() in line for t in terminal_types):
        return False
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(event, dict):
        return False
    # Grok CLI uses ``event`` (not ``type``) as the event-type key in some output shapes.
    return event.get("type") in terminal_types or (
        agent_type == AgentType.GROK and event.get("event") in terminal_types
    )


# ---------------------------------------------------------------------------
# Per-agent-type JSONL/stream output parsing
# ---------------------------------------------------------------------------


def _extract_session_id_from_jsonl(raw: str) -> str | None:
    for event in _iter_json_events(raw):
        for key in ("session_id", "thread_id"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _maybe_parse_usage(line: bytes, current: _UsageTotals) -> _UsageTotals:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return current

    usage: object = {}
    if event.get("type") == "result":
        usage = event.get("usage", {})
    elif event.get("type") == "assistant":
        message = event.get("message", {})
        usage = message.get("usage", {}) if isinstance(message, dict) else {}
    elif event.get("type") == "message_delta":
        usage = event.get("usage", {})

    if not isinstance(usage, dict):
        return current
    parsed = _usage_totals_from_dict(usage, input_includes_cache=False)
    # Claude Code stamps an authoritative ``total_cost_usd`` on the terminal
    # ``result`` event. Prefer it over token-derivation: it bills the exact model
    # and the 5m/1h ephemeral-cache tiers the static pricing table can't see, and
    # was observed ~2x higher than the token-derived figure (dashboard undercount).
    if event.get("type") == "result":
        reported = event.get("total_cost_usd")
        if isinstance(reported, int | float) and not isinstance(reported, bool) and reported > 0:
            parsed = replace(parsed, reported_cost=float(reported))
    return _max_usage(current, parsed)


def _extract_text_from_codex_jsonl(raw: str) -> tuple[str, _UsageTotals, str | None]:
    session_id: str | None = None
    usage_totals = _UsageTotals()
    turn_count = 0
    max_turn_input_tokens = 0
    messages: list[str] = []

    for event in _iter_json_events(raw):
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                session_id = thread_id
            continue

        if event.get("type") in {"turn.completed", "token_count"}:
            usage = event.get("usage" if event.get("type") == "turn.completed" else "info", {})
            if isinstance(usage, dict):
                turn_count += 1
                parsed = _usage_totals_from_dict(usage, input_includes_cache=True)
                max_turn_input_tokens = max(
                    max_turn_input_tokens,
                    parsed.max_turn_input_tokens,
                )
                usage_totals = _max_usage(usage_totals, parsed)
            continue

        if event.get("type") != "item.completed":
            continue
        item = event.get("item", {})
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str):
            messages.append(text)

    usage_totals = _UsageTotals(
        tokens_in=usage_totals.tokens_in,
        tokens_out=usage_totals.tokens_out,
        cached_tokens_in=usage_totals.cached_tokens_in,
        cache_write_tokens_in=usage_totals.cache_write_tokens_in,
        turn_count=turn_count,
        max_turn_input_tokens=max_turn_input_tokens,
    )
    return (messages[-1] if messages else raw), usage_totals, session_id


def _extract_text_from_grok_jsonl(raw: str) -> tuple[str, _UsageTotals, str | None]:
    """Parse Grok CLI JSONL output.  Delegates to the narrow Grok parser."""
    return cli_grok.parse_grok_jsonl(raw)


def _extract_text_from_stream_json(raw: str) -> str:
    last_result: str | None = None
    for event in _iter_json_events(raw):
        if event.get("type") == "result" and "result" in event:
            last_result = str(event["result"])
    if last_result is not None:
        return last_result

    parts: list[str] = []
    for event in _iter_json_events(raw):
        if event.get("type") == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", []) if isinstance(msg, dict) else []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
        elif event.get("type") == "content_block_delta":
            delta = event.get("delta", {})
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                parts.append(str(delta.get("text", "")))
    return "".join(parts)


def _parse_claude_output(raw: str) -> tuple[str, _UsageTotals, str | None]:
    """Parse a Claude Code stream-json transcript into text/usage/session id."""
    usage = _UsageTotals()
    for line in map(str.strip, raw.splitlines()):
        if line:
            usage = _maybe_parse_usage(line.encode("utf-8"), usage)
    return _extract_text_from_stream_json(raw), usage, _extract_session_id_from_jsonl(raw)


def _extract_text_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_extract_text_value(item) for item in value]
        return "".join(part for part in parts if part)
    if not isinstance(value, dict):
        return None

    for key in ("text", "content", "response", "result"):
        text = _extract_text_value(value.get(key))
        if text:
            return text

    value_parts = value.get("parts")
    if isinstance(value_parts, list):
        text_parts = [_extract_text_value(part) for part in value_parts]
        return "".join(part for part in text_parts if part)
    return None


class CliOutputFormat(Protocol):
    """A per-agent-type parser: raw stdout -> (text, usage totals, session id)."""

    def parse(self, raw: str) -> tuple[str, _UsageTotals, str | None]: ...


@dataclass(frozen=True, slots=True)
class _FunctionFormat:
    """Adapt a free parse function into the :class:`CliOutputFormat` protocol."""

    _parse: Callable[[str], tuple[str, _UsageTotals, str | None]]

    def parse(self, raw: str) -> tuple[str, _UsageTotals, str | None]:
        return self._parse(raw)


# Registry: adding a fourth agent type is one entry here, not an if/elif edit
# in ``_read_output``.
_PARSERS: dict[AgentType, CliOutputFormat] = {
    AgentType.CLAUDE_CODE: _FunctionFormat(_parse_claude_output),
    AgentType.CODEX: _FunctionFormat(_extract_text_from_codex_jsonl),
    AgentType.GROK: _FunctionFormat(_extract_text_from_grok_jsonl),
}


async def _kill_process(proc: asyncio.subprocess.Process, agent_id: str) -> None:
    """Send SIGTERM, wait up to _SIGKILL_GRACE seconds, then SIGKILL."""
    if not hasattr(os, "killpg"):
        # Windows: no process groups. ``start_new_session=True`` is a no-op
        # there, so there is nothing to ``os.killpg`` and ``os.getpgid`` is
        # absent entirely. Tear the tree down by PID via taskkill instead.
        if proc.pid is None:
            _close_process_transport(proc)
            return
        subprocess_env.kill_tree_sync(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=float(_SIGKILL_GRACE))
        except TimeoutError:
            _logger.warning("sending_sigkill", agent_id=agent_id)
            subprocess_env.kill_tree_sync(proc.pid)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=float(_SIGKILL_GRACE))
        if proc.returncode is None:
            _logger.warning(
                "taskkill_failed",
                agent_id=agent_id,
                pid=proc.pid,
            )
        _close_process_transport(proc)
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, TypeError):
        # ProcessLookupError: the process already exited. TypeError: proc.pid is
        # None (subprocess never spawned). Either way there is no group to kill.
        _close_process_transport(proc)
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        _close_process_transport(proc)
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=float(_SIGKILL_GRACE))
    except TimeoutError:
        _logger.warning("sending_sigkill", agent_id=agent_id)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        await proc.wait()
    finally:
        try:
            os.killpg(pgid, 0)
            ps = await asyncio.create_subprocess_exec(
                "ps",
                "-g",
                str(pgid),
                "-o",
                "pid=",
                # Never inherit the sidecar's stdin (the live Tauri JSON-RPC
                # pipe); a child probing it can wedge teardown (#155).
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await ps.communicate()
            survivors = [
                int(line.strip())
                for line in stdout.decode("utf-8", errors="ignore").splitlines()
                if line.strip().isdigit()
            ]
            _logger.warning(
                "subprocess_zombie_detected",
                agent_id=agent_id,
                pgid=pgid,
                survivors=survivors,
            )
        except ProcessLookupError:
            pass
        _close_process_transport(proc)


def _close_streams(proc: asyncio.subprocess.Process) -> None:
    """Signal EOF on open pipes so the transport can be GC'd cleanly."""
    if proc.stdout is not None:
        with contextlib.suppress(Exception):
            proc.stdout.feed_eof()
    if proc.stderr is not None:
        with contextlib.suppress(Exception):
            proc.stderr.feed_eof()


def _close_process_transport(proc: asyncio.subprocess.Process) -> None:
    """Close asyncio's subprocess transport when it has not closed itself."""
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()
