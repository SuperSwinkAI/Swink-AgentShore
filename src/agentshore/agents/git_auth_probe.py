"""Pre-launch per-identity git-remote auth probing.

``preflight_identities`` validates that each configured GitHub *token* is
accepted by the GitHub *API* (``gh api user``) and is scoped to the repo. It
does NOT prove that the same identity can authenticate to the repo's *git
remote* over the actual transport a CLI agent will use (HTTPS extraheader or
SSH). A token that the API accepts can still fail ``git push``/``git fetch``
when, e.g., the keychain holds a stale credential, an SSH key is not loaded, or
the remote is an SSH URL the identity's key cannot reach. When that happens
mid-run, the CLI agent's ``git`` wedges on a credential prompt until the
dispatch timeout — exactly the silent-hang failure mode this module exists to
surface at launch instead.

This is the git-transport sibling of ``auth_probe.py`` (CLI-agent backend
auth): same conservative shape, same status vocabulary, same
``blocks_launch`` discipline. Only a *definitive* auth failure gates a launch;
timeouts / unexpected errors / an unprobeable remote are surfaced but never
strand an otherwise-fine session.

The probe is read-only (``git ls-remote``), short-timeout, and fully
non-interactive: it runs under the identity's resolved env overlay merged with
:func:`agentshore.subprocess_env.git_auth_config_overlay` (token → HTTPS Basic
header) plus ``GIT_TERMINAL_PROMPT=0``, so a bad token 401-fails fast rather
than prompting. The token is never logged — only the identity name, status, and
a short detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentshore import command, subprocess_env
from agentshore.error_markers import GIT_AUTH_FAILED_MARKERS as _AUTH_FAILED_MARKERS
from agentshore.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.config.models import RuntimeConfig

_logger = get_logger(__name__)

# Shared status vocabulary, mirroring ``auth_probe.py``'s spirit. A status here
# maps 1:1 to how the launch gate / banner classifies the identity.
GIT_AUTH_OK = "ok"
GIT_AUTH_FAILED = "auth_failed"
GIT_AUTH_TIMEOUT = "timeout"
GIT_AUTH_ERROR = "error"
GIT_AUTH_UNPROBEABLE = "unprobeable"

# Only a definitive auth failure gates a launch. ``timeout``/``error`` (transient
# probe hiccup, git missing) and ``unprobeable`` (no origin remote, no token to
# probe with) are surfaced but NEVER blocking — a probe glitch must not strand
# a launch, and the mid-run credential-prompt backstop catches a real failure
# that slips through.
_BLOCKING_STATUSES = frozenset({GIT_AUTH_FAILED})

# git ls-remote is a single read round-trip; 15s is ample and keeps the launch
# gate responsive. A genuinely broken auth path 401-fails far faster than this
# under the non-interactive env.
DEFAULT_PROBE_TIMEOUT_S = 15.0

# Output markers indicating the remote rejected our credentials (vs. a network
# blip), matched case-insensitively against stdout+stderr, live in the single
# ``error_markers`` registry as ``GIT_AUTH_FAILED_MARKERS``, imported above.


@dataclass(frozen=True)
class GitAuthProbeResult:
    """Outcome of probing one identity's git-remote auth."""

    identity_name: str
    status: str
    detail: str
    remote: str = ""

    @property
    def ok(self) -> bool:
        """True when auth succeeded or the identity can't be probed (non-blocking)."""
        return self.status in (GIT_AUTH_OK, GIT_AUTH_UNPROBEABLE)

    @property
    def blocks_launch(self) -> bool:
        """True only for a definitive, launch-gating git-auth failure."""
        return self.status in _BLOCKING_STATUSES


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


def _is_ssh_remote(remote: str) -> bool:
    value = remote.strip()
    return (
        value.startswith("ssh://")
        or value.startswith("git@")
        or ("@" in value.split("/", 1)[0] and ":" in value and not value.startswith("http"))
    )


def resolve_origin_remote(project_path: Path) -> str | None:
    """Return the ``origin`` remote URL for *project_path*, or ``None`` if absent.

    Robust by design: a missing remote, a missing git binary, or any failure
    yields ``None`` so the caller maps it to an :class:`GitAuthProbeResult` with
    status ``unprobeable`` rather than blocking a launch.
    """
    result = command.git_sync(
        "remote",
        "get-url",
        "origin",
        cwd=project_path,
        timeout_seconds=10.0,
    )
    if not result.ok:
        return None
    remote = result.stdout.strip()
    return remote or None


def probe_git_auth(
    identity_name: str,
    identity_env: dict[str, str],
    *,
    remote: str,
    timeout: float = DEFAULT_PROBE_TIMEOUT_S,
) -> GitAuthProbeResult:
    """Probe one identity's ability to authenticate to *remote* non-interactively.

    Runs a read-only ``git ls-remote --heads <remote> HEAD`` under a hardened,
    non-interactive env: the identity's resolved overlay
    (``GH_TOKEN``/``GH_CONFIG_DIR``/``GIT_SSH_COMMAND``) merged with
    :func:`subprocess_env.git_auth_config_overlay` so the token authenticates as
    an HTTPS Basic header, plus ``GIT_TERMINAL_PROMPT=0``. For an SSH remote the
    identity's ``GIT_SSH_COMMAND`` (key path) carries auth instead of the token
    header.

    Never raises — every failure maps to a :class:`GitAuthProbeResult`. The
    token is never logged.
    """
    if not remote:
        return GitAuthProbeResult(
            identity_name, GIT_AUTH_UNPROBEABLE, "no origin remote to probe", remote
        )

    ssh_remote = _is_ssh_remote(remote)
    overlay: dict[str, str] = dict(identity_env)
    # GIT_TERMINAL_PROMPT=0 is the belt-and-suspenders non-interactive guard on
    # top of the hardened env's credential-prompt neutralisation, so a missing
    # credential fails fast rather than blocking on a prompt that never arrives.
    overlay["GIT_TERMINAL_PROMPT"] = "0"

    if not ssh_remote:
        # HTTPS remote: inject the identity's token as a Basic-auth extraheader so
        # `git` authenticates as THIS identity (multi-identity-safe). For an SSH
        # remote we rely on GIT_SSH_COMMAND from the overlay instead.
        token = identity_env.get("GH_TOKEN") or identity_env.get("GITHUB_TOKEN")
        if token:
            overlay.update(subprocess_env.git_auth_config_overlay(token))

    result = command.git_sync(
        "ls-remote",
        "--heads",
        remote,
        "HEAD",
        op_class="git.network",
        env_overlay=overlay,
        timeout_seconds=timeout,
    )

    if result.tool_missing:
        _logger.warning("git_auth_probe_git_missing", identity=identity_name)
        return GitAuthProbeResult(
            identity_name, GIT_AUTH_ERROR, "git CLI not found on PATH", remote
        )
    if result.timed_out:
        _logger.warning("git_auth_probe_timeout", identity=identity_name, remote=remote)
        return GitAuthProbeResult(
            identity_name,
            GIT_AUTH_TIMEOUT,
            f"git ls-remote timed out after {timeout:g}s",
            remote,
        )
    if result.ok:
        _logger.info("git_auth_probe_ok", identity=identity_name, remote=remote)
        return GitAuthProbeResult(identity_name, GIT_AUTH_OK, "authenticated", remote)

    combined = f"{result.stdout}\n{result.stderr}".lower()
    detail = _first_meaningful_line(result.stderr) or _first_meaningful_line(result.stdout)
    if any(marker in combined for marker in _AUTH_FAILED_MARKERS):
        _logger.warning(
            "git_auth_probe_auth_failed",
            identity=identity_name,
            remote=remote,
            detail=detail,
        )
        return GitAuthProbeResult(
            identity_name,
            GIT_AUTH_FAILED,
            detail or "git remote rejected credentials",
            remote,
        )

    # Non-zero exit with no auth marker: a transient/unexpected failure
    # (network blip, unreachable host). Surface it but never block the launch.
    _logger.warning(
        "git_auth_probe_error",
        identity=identity_name,
        remote=remote,
        returncode=result.returncode,
        detail=detail,
    )
    return GitAuthProbeResult(
        identity_name,
        GIT_AUTH_ERROR,
        f"git ls-remote exited {result.returncode}: {detail}"
        if detail
        else f"git ls-remote exited {result.returncode}",
        remote,
    )


def probe_all_identities(
    cfg: RuntimeConfig,
    *,
    project_path: Path,
    remote: str | None = None,
) -> list[GitAuthProbeResult]:
    """Probe every configured GitHub identity's git-remote auth independently.

    Each identity in ``cfg.identities`` is validated separately (multi-identity:
    one identity's broken credential never masks another's), mirroring how
    ``preflight_identities`` walks identities for gh-token validation. The
    ``origin`` remote is resolved from *project_path* unless *remote* is passed.
    Returns an empty list when no identities are configured.
    """
    if not cfg.identities:
        return []

    resolved_remote = remote if remote is not None else resolve_origin_remote(project_path)
    if not resolved_remote:
        # No origin remote to probe against — non-blocking unprobeable for each
        # configured identity so the banner still reports them.
        return [
            GitAuthProbeResult(name, GIT_AUTH_UNPROBEABLE, "no origin remote configured", "")
            for name in sorted(cfg.identities)
        ]

    from agentshore.agents.identity import resolve_identity_env
    from agentshore.config.models import AgentConfig

    results: list[GitAuthProbeResult] = []
    for name in sorted(cfg.identities):
        # Bind a transient agent to this identity so the shared resolver produces
        # the exact env overlay (token + GH_CONFIG_DIR + GIT_SSH_COMMAND) the
        # Agent Manager would inject when dispatching under this identity.
        try:
            identity_env = resolve_identity_env(cfg, AgentConfig(identity=name))
        except Exception as exc:  # noqa: BLE001 — token-resolution failures are a
            # GitHub-token concern surfaced by preflight_identities; never let one
            # raise here. Treat as non-blocking unprobeable.
            _logger.warning(
                "git_auth_probe_identity_resolution_failed",
                identity=name,
                error=str(exc)[:200],
            )
            results.append(
                GitAuthProbeResult(
                    name,
                    GIT_AUTH_UNPROBEABLE,
                    "could not resolve identity env",
                    resolved_remote,
                )
            )
            continue
        results.append(probe_git_auth(name, identity_env, remote=resolved_remote))
    return results
