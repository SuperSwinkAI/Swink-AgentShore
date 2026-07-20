"""Pre-launch CLI-agent backend auth probing.

``preflight_identities`` validates the *GitHub* identity tokens a session will
commit/merge with. It does NOT validate the *backend* auth each CLI agent uses
to reach its model provider — e.g. the Codex CLI's cached ``chatgpt.com``
session token, which carries a TTL and expires mid-run. When it expires the
Codex CLI prints ``failed to renew cache TTL`` / ``failed to refresh available
models`` to stderr and then hangs reading from stdin, so every dispatch runs to
the full ``stream_idle_timeout`` before being killed — observed burning 16
plays in a single session.

This module is the single source of truth for "is agent <type>'s backend auth
currently valid?", shared by three call sites so a green badge on the desktop
setup screen provably means the launch gate will pass:

* the CLI launch gate (``preflight_cli_agent_auth`` in ``session/bootstrap.py``),
* the desktop ``session.start`` gate (a phase in ``sidecar/session_lifecycle``),
* the desktop agents/identities setup screen (``agents.check_auth`` RPC).

The probe is intentionally conservative: agent types with a reliable,
non-mutating auth-status command (codex, swink-coding) are probed via that
command; agy is probed actively (it has no status verb). On Windows the agy probe must run under
a ConPTY (see ``agents/cli/conpty.py``): in ``-p`` mode agy blocks on a terminal
Device-Attributes query until the terminal replies, so over plain pipes it hangs
*regardless of auth* — which would make this probe a false-negative that gates
the agent off for everyone. Under the ConPTY agy proceeds, and a probe timeout
*then* means the authoritative logged-out hang (its interactive re-login). Every
other type returns ``UNPROBEABLE`` and never blocks a launch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentshore import subprocess_env
from agentshore.agents import cli_antigravity
from agentshore.agents.cli import conpty
from agentshore.error_markers import PROBE_NOT_AUTHED_MARKERS as _NOT_AUTHED_MARKERS
from agentshore.state import CLI_AGENT_TYPES, AgentType

if TYPE_CHECKING:
    from agentshore.config.models import AgentConfig, RuntimeConfig

# Shared status vocabulary. The desktop setup screen and the launch gate both
# consume these exact strings, so a status here maps 1:1 to a frontend badge.
AUTH_OK = "ok"
AUTH_EXPIRED = "expired"
AUTH_TIMEOUT = "timeout"
AUTH_ERROR = "error"
AUTH_UNPROBEABLE = "unprobeable"

# Only these statuses gate a launch. ``error`` (binary missing / unexpected
# non-zero with no auth marker) and ``timeout`` are surfaced but NOT blocking:
# a transient probe hiccup must never strand an otherwise-fine session, and the
# runtime auth-suppression backstop (ErrorClass.AUTH parking) catches a genuine
# failure that slips through.
_BLOCKING_STATUSES = frozenset({AUTH_EXPIRED})

# Default probe timeout. Auth-status is a local credential read; 10s is ample
# and keeps the setup screen / launch gate responsive.
DEFAULT_PROBE_TIMEOUT_S = 10.0

# Per-type auth-status command (args appended to the resolved binary). Only
# Codex and swink-coding expose a reliable, non-interactive, non-mutating
# status verb today; the others fall through to UNPROBEABLE until a
# trustworthy command is confirmed (a wrong probe that blocks launch is worse
# than no probe).
_PROBE_ARGV: dict[AgentType, tuple[str, ...]] = {
    AgentType.CODEX: ("login", "status"),
    AgentType.SWINK_CODING: ("auth", "status"),
}

_DEFAULT_BINARY: dict[AgentType, str] = {
    AgentType.CLAUDE_CODE: "claude",
    AgentType.CODEX: "codex",
    AgentType.GROK: "grok",
    AgentType.ANTIGRAVITY: "agy",
    AgentType.SWINK_CODING: "swink-coding",
}

# agy has no non-mutating status verb, and — unlike the other CLIs — a dead
# Antigravity OAuth session in ``-p`` mode does NOT error-and-exit: it drops into
# an interactive re-login prompt and HANGS at zero output until killed (observed
# 24 min). So a probe that reaches its timeout is the authoritative "logged out"
# signal. Probe actively with a trivial prompt (healthy ~3-10s); a wedged agy runs
# to the ceiling and is classified EXPIRED (launch-gating).
_ANTIGRAVITY_PROBE_PROMPT = "Reply with the single word OK and nothing else."

# Generous so a cold agy (language-server + model spin-up) never false-trips:
# healthy probes return in seconds, so this ceiling is only reached by a
# genuinely wedged (logged-out) agy.
ANTIGRAVITY_PROBE_TIMEOUT_S = 45.0

# Not-authenticated / dead-session markers (matched case-insensitively against
# stdout+stderr) live in the single ``error_markers`` registry as
# ``PROBE_NOT_AUTHED_MARKERS`` (imported above). They include the Codex TTL-expiry
# signatures so the same vocabulary classifying a mid-run hang (ErrorClass.AUTH)
# also classifies this pre-launch probe.


@dataclass(frozen=True)
class AuthProbeResult:
    """Outcome of probing one agent type's backend auth."""

    agent_type: AgentType
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        """True when auth is valid or the type can't be probed (non-blocking)."""
        return self.status in (AUTH_OK, AUTH_UNPROBEABLE)

    @property
    def blocks_launch(self) -> bool:
        """True only for a definitive, launch-gating auth failure."""
        return self.status in _BLOCKING_STATUSES


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


def probe_cli_auth(
    agent_type: AgentType,
    env: dict[str, str] | None = None,
    *,
    binary: str | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT_S,
) -> AuthProbeResult:
    """Probe one CLI agent type's backend auth via its status command.

    Runs a short, non-mutating auth-status subprocess under the ambient
    environment overlaid with *env*. Never raises — every failure mode maps to
    an :class:`AuthProbeResult`. Blocking in nature (uses ``subprocess.run``);
    async callers should wrap it in ``asyncio.to_thread``.
    """
    if agent_type == AgentType.ANTIGRAVITY:
        # agy needs an active liveness probe (no status verb; logged-out = hang)
        # with a longer ceiling than the generic status-command default. Honor an
        # explicitly-passed timeout (tests force a tiny one); otherwise use the
        # agy ceiling so a cold-but-healthy agy never false-trips.
        agy_timeout = ANTIGRAVITY_PROBE_TIMEOUT_S if timeout == DEFAULT_PROBE_TIMEOUT_S else timeout
        return _probe_antigravity_auth(env, binary=binary, timeout=agy_timeout)

    argv_tail = _PROBE_ARGV.get(agent_type)
    if argv_tail is None:
        return AuthProbeResult(
            agent_type, AUTH_UNPROBEABLE, "no auth-status probe for this agent type"
        )

    exe = binary or _DEFAULT_BINARY.get(agent_type, agent_type.value)
    resolved = shutil.which(exe)
    if resolved is None:
        return AuthProbeResult(agent_type, AUTH_ERROR, f"{exe!r} not found on PATH")

    full_env = {**os.environ, **(env or {})}
    try:
        # Popen (not subprocess.run) so a timeout can tree-kill: the probed CLIs
        # (codex) are node shims that spawn children; subprocess.run's own
        # timeout kill reaps only the direct child and leaves the node subtree
        # alive. CREATE_NO_WINDOW + new process group (Windows; 0 elsewhere)
        # suppresses the console flash / AV window-hooking latency this module
        # exists to avoid and roots the child in a killable group, matching the
        # dispatch path in cli_agent and the hardened runner in command.py.
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, resolved binary
            [resolved, *argv_tail],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Pin stdin (never inherit the parent's): the desktop sidecar's
            # stdin is the live Tauri JSON-RPC pipe, and the very CLIs we probe
            # (codex) wedge on a contended/empty stdin. Enforced by
            # tests/test_subprocess_stdin_guard.py.
            stdin=subprocess.DEVNULL,
            text=True,
            env=full_env,
            creationflags=subprocess_env.no_window_creationflags(),
        )
    except OSError as exc:
        return AuthProbeResult(agent_type, AUTH_ERROR, str(exc)[:200])

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the whole tree (codex → node), not just the direct child, so
        # nothing lingers past the probe.
        if proc.pid is not None:
            subprocess_env.kill_tree_sync(proc.pid)
        proc.kill()
        proc.communicate()
        return AuthProbeResult(agent_type, AUTH_TIMEOUT, f"auth probe timed out after {timeout:g}s")

    stdout = stdout or ""
    stderr = stderr or ""
    combined = f"{stdout}\n{stderr}".lower()
    if any(marker in combined for marker in _NOT_AUTHED_MARKERS):
        detail = _first_meaningful_line(stderr) or _first_meaningful_line(stdout)
        return AuthProbeResult(
            agent_type, AUTH_EXPIRED, detail or "backend session not authenticated"
        )
    if proc.returncode != 0:
        detail = _first_meaningful_line(stderr) or _first_meaningful_line(stdout)
        return AuthProbeResult(
            agent_type,
            AUTH_ERROR,
            f"auth probe exited {proc.returncode}: {detail}"
            if detail
            else f"auth probe exited {proc.returncode}",
        )
    return AuthProbeResult(agent_type, AUTH_OK, "authenticated")


def _probe_antigravity_auth(
    env: dict[str, str] | None,
    *,
    binary: str | None,
    timeout: float,
) -> AuthProbeResult:
    """Active liveness probe for agy's Antigravity OAuth session.

    Runs a trivial ``agy -p`` prompt under hardened (headless) env and a hard
    subprocess ceiling. A response classifies OK; a not-authed marker or a
    *timeout* (agy's logged-out interactive-relogin hang) classifies EXPIRED,
    which gates the launch. Mirrors the spawn-hardening of :func:`probe_cli_auth`
    (DEVNULL stdin, tree-kill on timeout, no-window creationflags).
    """
    at = AgentType.ANTIGRAVITY
    exe = binary or _DEFAULT_BINARY[at]
    resolved = shutil.which(exe)
    if resolved is None:
        return AuthProbeResult(at, AUTH_ERROR, f"{exe!r} not found on PATH")

    # Wind agy's own internal task wait down just under our hard ceiling so the
    # process is already tearing itself down as the outer timeout fires.
    inner_s = max(5, int(timeout) - 5)
    # ``--dangerously-skip-permissions`` matches the dispatch invocation and
    # auto-approves agy's permission/trust prompts so neither path stalls on one.
    argv = [
        resolved,
        "--print-timeout",
        f"{inner_s}s",
        "--dangerously-skip-permissions",
        "-p",
        _ANTIGRAVITY_PROBE_PROMPT,
    ]
    # Hardened, headless env (CI/NO_COLOR/TERM=dumb) matching the dispatch path so
    # the probe exercises the same conditions a real agy run sees.
    full_env = subprocess_env.hardened_env(overlay=env or {}, for_antigravity=True)

    returncode: int | None
    timed_out = False
    if conpty.should_use_conpty(at):
        # Windows: agy must run under a ConPTY or it hangs on its terminal
        # Device-Attributes query *regardless of auth*, which would make this
        # probe a false-negative that gates the agent off for everyone.
        try:
            raw_stdout, returncode, timed_out = conpty.run_sync(argv, env=full_env, timeout=timeout)
        except OSError as exc:
            return AuthProbeResult(at, AUTH_ERROR, str(exc)[:200])
        stdout = cli_antigravity.strip_ansi(raw_stdout)
        stderr = ""  # ConPTY merges stderr into the single stream
    else:
        try:
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, resolved binary
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                env=full_env,
                creationflags=subprocess_env.no_window_creationflags(),
            )
        except OSError as exc:
            return AuthProbeResult(at, AUTH_ERROR, str(exc)[:200])
        try:
            out, err = proc.communicate(timeout=timeout)
            stdout, stderr = out or "", err or ""
        except subprocess.TimeoutExpired:
            if proc.pid is not None:
                subprocess_env.kill_tree_sync(proc.pid)
            proc.kill()
            proc.communicate()
            timed_out = True
            stdout, stderr = "", ""
        returncode = proc.returncode

    if timed_out:
        return AuthProbeResult(
            at,
            AUTH_EXPIRED,
            f"agy produced no response within {timeout:g}s — its Antigravity OAuth "
            "session is expired (in -p mode it hangs on an interactive re-login). "
            "Re-authenticate by launching the Antigravity app or running `agy` once "
            "interactively.",
        )

    combined = f"{stdout}\n{stderr}".lower()
    if any(marker in combined for marker in _NOT_AUTHED_MARKERS):
        detail = _first_meaningful_line(stderr) or _first_meaningful_line(stdout)
        return AuthProbeResult(at, AUTH_EXPIRED, detail or "Antigravity session not authenticated")
    if returncode in (0, None) and stdout.strip():
        return AuthProbeResult(at, AUTH_OK, "authenticated")
    if returncode not in (0, None):
        detail = _first_meaningful_line(stderr) or _first_meaningful_line(stdout)
        return AuthProbeResult(
            at,
            AUTH_ERROR,
            f"agy auth probe exited {returncode}: {detail}"
            if detail
            else f"agy auth probe exited {returncode}",
        )
    # Clean exit but empty output: a rare agy no-op. Auth itself isn't disproven,
    # so surface it as a non-blocking error rather than gating the launch.
    return AuthProbeResult(at, AUTH_ERROR, "agy returned empty output (auth inconclusive)")


def configured_cli_agent_types(cfg: RuntimeConfig) -> list[tuple[AgentType, AgentConfig]]:
    """Return (type, config) for each enabled, probeable CLI agent in *cfg*.

    Disabled agents are skipped. One entry per type — a backend session token is
    shared across instances of a type, so probing it once is sufficient.
    """
    seen: set[AgentType] = set()
    out: list[tuple[AgentType, AgentConfig]] = []
    for name, agent_cfg in cfg.agents.items():
        try:
            agent_type = AgentType(name)
        except ValueError:
            continue
        if agent_type not in CLI_AGENT_TYPES or not agent_cfg.enabled:
            continue
        if agent_type in seen:
            continue
        seen.add(agent_type)
        out.append((agent_type, agent_cfg))
    return out


def probe_configured_cli_auth(cfg: RuntimeConfig) -> list[AuthProbeResult]:
    """Probe every enabled CLI agent type configured in *cfg*.

    Each probe runs under the agent's resolved GitHub identity env overlay (for
    parity with how the Agent Manager spawns it) and its configured ``binary``
    override. Shared by the CLI and desktop launch gates.
    """
    from agentshore.agents.identity import resolve_identity_env

    results: list[AuthProbeResult] = []
    for agent_type, agent_cfg in configured_cli_agent_types(cfg):
        try:
            env = resolve_identity_env(cfg, agent_cfg)
        except Exception:
            # Identity resolution failures are a GitHub-token concern surfaced by
            # preflight_identities; don't let one block the backend-auth probe.
            env = {}
        results.append(probe_cli_auth(agent_type, env, binary=agent_cfg.binary))
    return results
