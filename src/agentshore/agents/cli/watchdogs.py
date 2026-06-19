"""Async watchdog tasks and related dataclasses for the CLI agent adapter.

Extracted from ``cli_agent``: the ``_StdoutActivity`` / ``_StderrSniffer``
accumulators, the three watch-coroutines (stderr-auth, stream-idle,
first-byte), and the ``_await_output_or_timeout`` orchestrator that races them.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Final, NoReturn

from agentshore.agents.cli.errors import (
    _AUTH_PATTERNS,
    _CACHE_RENEWAL_MARKERS,
    _is_cache_renewal_stdin_hang,
    _is_transient_cache_blip,
)
from agentshore.errors import ErrorClass, PlayTimeoutError
from agentshore.logging import get_logger
from agentshore.state import AgentType

_logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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

# Effectively-infinite sleep used to park the first-byte watchdog once it has
# handed off to the idle watcher; the caller always cancels the task on read
# completion, so this never actually elapses.
_NEVER_S: Final[float] = 365 * 24 * 3600.0

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ReadOutputFailed:
    exc: BaseException


@dataclass(frozen=True, slots=True)
class _DispatchArgv:
    """Packaged argv + derived log fields produced by ``_build_dispatch_argv``."""

    argv: list[str]
    prompt_bytes: int
    argv_str: str  # truncated preview for ``cli_dispatch_start`` log event


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
            # EOF-parse variant; a bare cache-renewal line still trips. #231:
            # if the EOF shape is followed by Codex's stdin-closed write failure,
            # the dispatch is unrecoverably hung and must fast-fail as auth.
            cache_stdin_hang = _is_cache_renewal_stdin_hang(lowered)
            if hard_auth or (
                cache_auth and (not _is_transient_cache_blip(lowered) or cache_stdin_hang)
            ):
                self.auth_hit = True
                return True
        return False


# ---------------------------------------------------------------------------
# Async watchdog coroutines
# ---------------------------------------------------------------------------


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
