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
    _ENOSPC_PATTERNS,
    _RATE_LIMIT_PATTERNS,
    _is_cache_renewal_stdin_hang,
    _is_transient_cache_blip,
)
from agentshore.errors import ErrorClass, PlayTimeoutError
from agentshore.logging import get_logger
from agentshore.state import AgentType

_logger = get_logger(__name__)


_POST_RESPONSE_GRACE_S: Final[float] = 60.0

_FIRST_BYTE_DEADLINE_S: Final[float] = 600.0

_FIRST_BYTE_DEADLINE_BY_TYPE: dict[AgentType, float] = {
    AgentType.ANTIGRAVITY: 1800.0,
    AgentType.SWINK_CODING: 60.0,
    AgentType.COPILOT: 60.0,
}

_NEVER_S: Final[float] = 365 * 24 * 3600.0


@dataclass(frozen=True, slots=True)
class _ReadOutputFailed:
    exc: BaseException


@dataclass(frozen=True, slots=True)
class _DispatchArgv:
    """Packaged argv + derived log fields produced by ``_build_dispatch_argv``."""

    argv: list[str]
    prompt_bytes: int
    argv_str: str


@dataclass(slots=True)
class _StdoutActivity:
    last_stdout_at: float
    received_any: bool = False
    response_complete: bool = False
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
       mislabelled TIMEOUT_STREAM_IDLE. An ``_ENOSPC_PATTERNS`` match (host disk
       exhaustion) is checked first and suppresses the auth abort even when a
       cache-renewal marker coexists in the tail (#332) — a full disk is not an
       auth failure and must fall through to natural exit, where
       ``_classify_error`` correctly buckets it as ``CRASH_ENOSPC``.
    """

    tail_window: int = 8192
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
            if any(p in lowered for p in _ENOSPC_PATTERNS):
                return False
            if any(p in lowered for p in _RATE_LIMIT_PATTERNS):
                return False
            hard_auth = any(p in lowered for p in _AUTH_PATTERNS if p not in _CACHE_RENEWAL_MARKERS)
            cache_auth = any(p in lowered for p in _CACHE_RENEWAL_MARKERS)
            cache_stdin_hang = _is_cache_renewal_stdin_hang(lowered)
            if hard_auth or (
                cache_auth and (not _is_transient_cache_blip(lowered) or cache_stdin_hang)
            ):
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
