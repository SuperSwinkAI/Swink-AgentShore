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

The probe is intentionally conservative: only agent types with a reliable,
non-mutating auth-status command are probed; everything else returns
``UNPROBEABLE`` and never blocks a launch, so this can never introduce a
false-negative startup failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentshore import subprocess_env
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

# Per-type auth-status command (args appended to the resolved binary). Only the
# Codex CLI exposes a reliable, non-interactive, non-mutating status verb today;
# the others fall through to UNPROBEABLE until a trustworthy command is
# confirmed (a wrong probe that blocks launch is worse than no probe).
_PROBE_ARGV: dict[AgentType, tuple[str, ...]] = {
    AgentType.CODEX: ("login", "status"),
}

_DEFAULT_BINARY: dict[AgentType, str] = {
    AgentType.CLAUDE_CODE: "claude",
    AgentType.CODEX: "codex",
    AgentType.GROK: "grok",
    AgentType.ANTIGRAVITY: "agy",
}

# Output markers indicating the backend is NOT authenticated / the cached
# session is dead. Matched case-insensitively against stdout+stderr. Includes
# the Codex TTL-expiry signatures so the same vocabulary that classifies a
# mid-run hang (ErrorClass.AUTH) also classifies a pre-launch probe.
_NOT_AUTHED_MARKERS: tuple[str, ...] = (
    "not logged in",
    "not authenticated",
    "logged out",
    "no credentials",
    "please run",
    "run `codex login`",
    "run 'codex login'",
    "failed to renew cache ttl",
    "failed to refresh available models",
)


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
