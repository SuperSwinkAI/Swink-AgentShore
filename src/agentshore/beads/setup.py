"""Beads project setup helpers called by `agentshore init`.

Three steps, in order:
  1. ensure_bd_installed()        — verify `bd` is on PATH
  2. bd_init_project(path)        — run `bd init` if .beads/ is absent
  3. bd_setup_for_agent_types(...)— run `bd hooks install` for git integration

These are synchronous-safe wrappers that delegate to asyncio subprocesses
via the helpers in agentshore.beads. They are intentionally kept separate from
the core beads module so the CLI can import them without pulling in the full
async graph-loading machinery.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import structlog
import yaml

from agentshore.beads import (
    BdError,
    BeadsSchemaDriftError,
    bd,
    is_schema_drift_error,
    resolve_bd_binary,
)
from agentshore.state import AgentType

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger(__name__)

# Supply-chain + change-control pin for the `bd` (beads) binary. bd's CLI
# semantics directly shape the beads graph — e.g. `bd link`'s default
# dependency type changed across releases and silently inverted epic/task
# linkage (blocking every leaf task → zero implementation work). Pinning the
# binary means such a change can't slip in unannounced. Override with the
# AGENTSHORE_BD_VERSION env var (set it empty to disable the check) only after
# re-verifying the skill-template `bd` calls against the new release.
REQUIRED_BD_VERSION = "1.1.0"


def _check_bd_version(bd_binary: str) -> None:
    """Assert the resolved bd binary matches the pinned version.

    Raises RuntimeError on mismatch. Honours AGENTSHORE_BD_VERSION as an
    override (empty value disables the check entirely).
    """
    expected = os.environ.get("AGENTSHORE_BD_VERSION", REQUIRED_BD_VERSION).strip()
    if not expected:
        return
    try:
        completed = subprocess.run(
            [bd_binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            # Never inherit the sidecar's stdin (the live Tauri JSON-RPC pipe);
            # a subprocess probing it can wedge session startup (#155).
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"could not determine bd version via `{bd_binary} --version`: {exc}"
        ) from exc
    match = re.search(r"\d+\.\d+\.\d+", completed.stdout or completed.stderr or "")
    found = match.group(0) if match else (completed.stdout or "").strip()
    if found != expected:
        raise RuntimeError(
            f"bd version {found!r} does not match AgentShore's pinned version {expected!r}. "
            "bd is pinned because its CLI semantics affect the beads graph (e.g. `bd link`'s "
            "default dependency type changed between releases and silently broke epic/task "
            "linkage). Install bd "
            f"{expected} from https://github.com/gastownhall/beads, or set AGENTSHORE_BD_VERSION "
            "to override after re-verifying the skill-template `bd` calls."
        )


# Map AgentType enum values to the bd hooks actor name.  API-only agents
# have no bd setup target and are omitted.
_BD_ACTOR_NAMES: dict[AgentType, str] = {
    AgentType.CLAUDE_CODE: "claude",
    AgentType.CODEX: "codex",
    AgentType.GROK: "grok",
}


def ensure_bd_installed() -> None:
    """Verify that `bd` is on PATH and matches the pinned version.

    If bd is absent, delegates to ``downloader.provision_bd`` which enforces
    the consent gate: interactive sessions may prompt the user; headless
    sessions fail with instructions unless ``AGENTSHORE_AUTO_INSTALL_BD=1``
    is set. This function never silently downloads a binary in headless mode.

    Raises RuntimeError with install instructions if bd is not found (and
    consent for download is absent), or if its version does not match
    REQUIRED_BD_VERSION (see AGENTSHORE_BD_VERSION override). This check is
    intentionally synchronous so it can be called from the Click-based
    `agentshore init` command without an event loop.
    """
    from agentshore.beads.downloader import provision_bd

    # provision_bd returns the existing bd path when already installed,
    # downloads + returns the installed path when consent is present, or raises
    # with instructions when bd is absent and no consent is given (the
    # headless-fail invariant).
    bd_binary = resolve_bd_binary()
    if bd_binary is None:
        bd_binary = provision_bd(REQUIRED_BD_VERSION)
    if bd_binary is None:
        # Reachable only when the download was attempted and failed (best-effort
        # path returns None); the no-consent path raises inside provision_bd.
        raise RuntimeError(
            "The bd binary was not found. Set AGENTSHORE_BD_BIN to a bundled binary or install "
            "bd from https://github.com/gastownhall/beads and re-run agentshore init."
        )
    _check_bd_version(bd_binary)
    _warn_on_bd_path_skew(bd_binary)
    _logger.info("bd_available", path=bd_binary, required_version=REQUIRED_BD_VERSION)


def _warn_on_bd_path_skew(bd_binary: str) -> None:
    """Log when the ambient-PATH ``bd`` differs from the pinned/resolved *bd_binary*.

    The desktop app pins a bundled sidecar bd via ``AGENTSHORE_BD_BIN``
    (``resolve_bd_binary`` prefers it), but a bare ``bd`` typed in a terminal
    — or run by anything that doesn't go through AgentShore's own resolution
    — still follows the ambient PATH and can land on a different, unpinned
    install. Two writers at different schema versions against the same
    embedded Dolt store is a direct schema-drift vector (this is exactly what
    happened in practice: PATH bd was 1.0.4 while the pinned sidecar was
    1.1.0, and the two eventually disagreed badly enough that the newer
    binary could not even open the store).

    This function only detects and logs the skew — it does not rewrite the
    user's shell PATH or reinstall their standalone bd, which isn't something
    that can be done invisibly/safely. The one vector this module *can* and
    does fix invisibly is git hooks (see ``_pin_bd_in_hook_scripts``), since
    those are AgentShore-managed files, not the user's own environment.
    """
    path_bd = shutil.which("bd")
    if path_bd is None:
        return
    with contextlib.suppress(OSError):
        if os.path.samefile(path_bd, bd_binary):
            return
    try:
        completed = subprocess.run(
            [path_bd, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return
    match = re.search(r"\d+\.\d+\.\d+", completed.stdout or completed.stderr or "")
    path_version = match.group(0) if match else (completed.stdout or "").strip()
    expected = os.environ.get("AGENTSHORE_BD_VERSION", REQUIRED_BD_VERSION).strip()
    if path_version == expected:
        return  # different binary, same version — not a drift risk
    _logger.warning(
        "bd_path_skew_detected",
        resolved_bd=bd_binary,
        resolved_version=expected,
        path_bd=path_bd,
        path_bd_version=path_version,
        hint=(
            "ambient PATH bd differs from AgentShore's pinned binary; bd git hooks are "
            "routed around this automatically, but a `bd` typed manually in a shell still "
            f"resolves the ambient one. Consider updating it to {expected} from "
            "https://github.com/gastownhall/beads."
        ),
    )


# Opt-in env var for headless/CI consent to the one dangerous recovery path:
# migrating a *remote-backed* store's schema and pushing it. Mirrors
# AGENTSHORE_AUTO_INSTALL_BD's shape (downloader.py) — must be explicitly set,
# default is conservative (never silently fork a shared schema).
_ALLOW_REMOTE_MIGRATE_ENV_VAR = "AGENTSHORE_ALLOW_REMOTE_MIGRATE"

_REMOTE_MIGRATE_COMMAND = "BD_ALLOW_REMOTE_MIGRATE=1 bd migrate && bd dolt push"


def _beads_store_has_remote(project_path: Path) -> bool:
    """True when ``.beads/config.yaml`` declares a ``sync.remote``.

    A purely local (never-shared) beads store can always migrate itself
    safely — there is nothing to fork against. Read directly from the YAML
    file rather than shelling out to ``bd context``/``bd info``: both have
    been observed to warn or misbehave on this exact repo's config (a
    duplicate ``sync.remote`` key — both the flat ``sync.remote:`` form and
    the nested ``sync: {remote: ...}`` form present in the same file, a
    config-format drift artifact of its own) even though PyYAML parses it
    fine (last-key-wins, no error). Best-effort: a missing/unreadable file or
    parse error is treated as "no remote" (nothing more this function can
    safely infer), never raised.
    """
    config_path = project_path / ".beads" / "config.yaml"
    if not config_path.is_file():
        return False
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        _logger.warning("beads_config_read_failed", project_path=str(project_path), error=str(exc))
        return False
    if not isinstance(config, dict):
        return False
    if config.get("sync.remote"):
        return True
    sync_section = config.get("sync")
    return bool(isinstance(sync_section, dict) and sync_section.get("remote"))


def _remote_migrate_consented(*, assume_yes: bool) -> bool:
    """Consent gate for the one action that can unrecoverably fork a shared schema.

    Mirrors ``downloader.provision_bd``'s consent shape: an explicitly
    consented caller (``assume_yes``), the opt-in env var, or an interactive
    TTY confirmation. Everywhere else in this module auto-heals silently —
    this is the deliberate exception, because bd's own remote-migrate refusal
    is explicitly a human coordination decision (its error payload marks it
    ``human_decision_required: true``): only one clone may ever run this, and
    nothing in the local state can prove which one that is.
    """
    if assume_yes:
        return True
    if os.environ.get(_ALLOW_REMOTE_MIGRATE_ENV_VAR, "").strip() == "1":
        _logger.info("beads_remote_migrate_env_consent", env_var=_ALLOW_REMOTE_MIGRATE_ENV_VAR)
        return True
    if sys.stdin.isatty():
        try:
            answer = (
                input(
                    "\nThis beads store is behind its remote's schema and bd's safe recovery "
                    "(bootstrap) could not catch it up. If — and only if — this is the single "
                    "machine designated to migrate the shared schema, AgentShore can run:\n"
                    f"  {_REMOTE_MIGRATE_COMMAND}\n"
                    "Running this from more than one clone forks the schema unrecoverably. "
                    "Proceed? [y/N] "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            answer = ""
        return answer in ("y", "yes")
    return False


async def reconcile_beads_schema(project_path: Path, *, assume_yes: bool = False) -> None:
    """Detect and — wherever it is safe to do so silently — heal beads schema drift.

    Generalizes the original #316 stale-remote-clone recovery into a full
    schema-drift preflight. The guiding principle is invisibility: every
    state below except one resolves with no user interaction (at most a log
    line). The one exception is the single action that can unrecoverably
    fork a shared schema — migrating a *remote-backed* store — which bd's own
    design marks ``human_decision_required: true`` because no local signal
    can prove this is the sole designated-migrator clone.

    States, in order:
      1. Healthy — the probe read succeeds. No-op.
      2. Remote-backed, behind — bd's "refusing to auto-apply ... to a
         remote-backed database" signature (``is_schema_drift_error``).
         Always try the safe ``bd bootstrap`` adopt path first (silent) — in
         practice this resolves the common case (another clone already
         migrated and pushed). If the signature persists after bootstrap,
         fall through to the consent gate for the dangerous migrate+push
         path; declining leaves it logged and raises ``BeadsSchemaDriftError``
         with the exact remediation commands so callers can surface (not
         swallow) a drift that nothing here could fix.
      3. Local-only, behind — no ``sync.remote`` configured
         (``_beads_store_has_remote``), so a plain ``bd migrate`` cannot fork
         anything. Runs automatically and silently.
      4. Unrecognized failure — logged only; this function isn't equipped to
         diagnose it, and the normal ``load_graph`` retry path already
         surfaces it through the ordinary ``GraphReadError``/error-line
         channels.

    *assume_yes* is for already-consented callers (e.g. an interactive
    installer wizard that already confirmed with the user); everywhere else
    consent for state 2's dangerous path comes from the
    ``AGENTSHORE_ALLOW_REMOTE_MIGRATE`` env var or an interactive prompt (see
    ``_remote_migrate_consented``).
    """
    try:
        await bd("list", "--all", "--json", "--limit", "0", cwd=project_path)
        return  # healthy store — nothing to reconcile
    except BdError as exc:
        probe_error = str(exc)

    if is_schema_drift_error(probe_error):
        _logger.warning(
            "beads_stale_remote_clone_detected",
            project_path=str(project_path),
            error=probe_error,
        )
        try:
            await bd("bootstrap", cwd=project_path)
        except BdError as exc:
            _logger.warning(
                "beads_bootstrap_recovery_failed",
                project_path=str(project_path),
                error=str(exc),
            )
        else:
            try:
                await bd("list", "--all", "--json", "--limit", "0", cwd=project_path)
            except BdError as exc:
                probe_error = str(exc)  # still drifted — fall through to the consent gate
            else:
                _logger.info("beads_bootstrap_recovery_ran", project_path=str(project_path))
                return

        if not _remote_migrate_consented(assume_yes=assume_yes):
            _logger.warning(
                "beads_remote_migrate_declined",
                project_path=str(project_path),
                remediation=_REMOTE_MIGRATE_COMMAND,
            )
            raise BeadsSchemaDriftError(
                "beads store is behind its remote's schema and `bd bootstrap` could not catch "
                f"it up. On exactly ONE machine (the designated migrator), run: "
                f"{_REMOTE_MIGRATE_COMMAND}\nOr set {_ALLOW_REMOTE_MIGRATE_ENV_VAR}=1 to consent "
                f"non-interactively. Original error: {probe_error}"
            )

        _logger.warning("beads_remote_migrate_consented", project_path=str(project_path))
        try:
            await bd("migrate", cwd=project_path, env_overlay={"BD_ALLOW_REMOTE_MIGRATE": "1"})
            await bd("dolt", "push", cwd=project_path)
        except BdError as exc:
            raise BeadsSchemaDriftError(
                f"consented remote migrate+push failed for {project_path}: {exc}"
            ) from exc
        _logger.info("beads_remote_migrate_ran", project_path=str(project_path))
        return

    if not _beads_store_has_remote(project_path):
        # No remote configured — a local schema mismatch can't fork a shared
        # store, so it's always safe to migrate automatically and silently.
        try:
            await bd("migrate", cwd=project_path)
        except BdError as exc:
            _logger.warning(
                "beads_local_migrate_failed", project_path=str(project_path), error=str(exc)
            )
            raise BeadsSchemaDriftError(
                f"bd migrate failed for local-only store {project_path}: {exc}"
            ) from exc
        _logger.info("beads_local_migrate_ran", project_path=str(project_path))
        return

    _logger.warning(
        "beads_graph_probe_failed_at_session_start",
        project_path=str(project_path),
        error=probe_error,
    )


async def _configure_dolt_auto_commit(project_path: Path) -> None:
    """Set the ``dolt.auto-commit`` config key so every writer commits per write.

    ``--dolt-auto-commit`` defaults to off on the bd CLI. AgentShore's own
    mutations pass ``--dolt-auto-commit=on`` per call, but agent-driven writes
    prescribed by skill templates do not, leaving uncommitted changes in the
    Dolt working set — historically a source of blocked schema migrations.
    Setting the config key once at init makes every writer (orchestrator and
    agents alike) commit per write without a per-call flag. Idempotent and
    best-effort: a failure here is logged and never blocks `agentshore init`,
    matching the ``bd hooks install`` step below.
    """
    try:
        await bd("config", "set", "dolt.auto-commit", "on", cwd=project_path)
        _logger.info("bd_dolt_auto_commit_configured", project_path=str(project_path))
    except BdError as exc:
        _logger.warning("bd_dolt_auto_commit_config_failed", error=str(exc))


async def bd_init_project(project_path: Path) -> None:
    """Run `bd init` in *project_path* if the beads store does not yet exist.

    Idempotent — if ``.beads/`` already exists, `bd init` itself is a no-op,
    but the ``dolt.auto-commit`` config is still applied so existing projects
    (created before this config step existed) pick it up too.
    Also writes ``.beads/.gitignore`` containing ``*`` so the local bead
    store is never committed to version control.
    """
    beads_dir = project_path / ".beads"
    if beads_dir.exists():
        _logger.info("bd_init_skipped", reason="already_initialised", path=str(beads_dir))
        await _configure_dolt_auto_commit(project_path)
        return

    _logger.info("bd_init_running", project_path=str(project_path))
    await bd("init", cwd=project_path)
    _logger.info("bd_init_done", path=str(beads_dir))

    # Ensure the store is gitignored.
    gitignore = beads_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")

    await _configure_dolt_auto_commit(project_path)


def _pin_bd_in_hook_scripts(project_path: Path, bd_binary: str) -> None:
    """Rewrite installed bd git hooks to invoke the pinned bd by absolute path.

    ``bd hooks install`` writes shell scripts (default target ``.beads/hooks/``,
    wired via git's ``core.hooksPath``) that resolve bd through
    ``command -v bd`` / a bare ``bd hooks run ...`` — i.e. whatever ``bd`` the
    hook's ambient shell PATH finds when git invokes it, independently of
    ``resolve_bd_binary()``. When that differs from the pinned binary (see
    ``_warn_on_bd_path_skew``), a commit-time hook silently runs a
    version-skewed bd against the same embedded Dolt store AgentShore itself
    just wrote — a direct schema-drift vector, and the one concretely
    observed in practice. Absolute-path invocation sidesteps PATH order
    entirely, so this fixes the vector without touching the user's shell
    environment.

    Patches the ``command -v bd`` resolution check and every ``bd hooks run``
    invocation inside bd's own "BEGIN/END BEADS INTEGRATION" marker block to
    reference *bd_binary* directly. Idempotent and best-effort: ``bd hooks
    install`` regenerates these files from scratch on every run, so this is
    called after every install; a hook file that doesn't match the expected
    template (e.g. a future bd release changing it) is left untouched and
    logged rather than corrupted.
    """
    hooks_dir = project_path / ".beads" / "hooks"
    if not hooks_dir.is_dir():
        return
    quoted = bd_binary.replace('"', '\\"')
    pinned = 0
    for hook_path in sorted(hooks_dir.glob("*")):
        if not hook_path.is_file():
            continue
        try:
            original = hook_path.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.warning("bd_hook_pin_read_failed", hook=str(hook_path), error=str(exc))
            continue
        if "BEGIN BEADS INTEGRATION" not in original:
            continue  # not a bd-generated hook — leave it alone
        patched = original.replace(
            "command -v bd >/dev/null 2>&1", f'command -v "{quoted}" >/dev/null 2>&1'
        ).replace("bd hooks run", f'"{quoted}" hooks run')
        if patched == original:
            continue  # already pinned, or a template shape this doesn't recognize
        try:
            hook_path.write_text(patched, encoding="utf-8")
        except OSError as exc:
            _logger.warning("bd_hook_pin_write_failed", hook=str(hook_path), error=str(exc))
            continue
        pinned += 1
    if pinned:
        _logger.info("bd_hooks_pinned", project_path=str(project_path), count=pinned)


async def bd_setup_for_agent_types(
    project_path: Path,
    enabled_agent_types: set[AgentType],
) -> list[str]:
    """Install bd git hooks for agent-identity tracking.

    Runs ``bd hooks install`` once for the project (bd v1.0.x has a single
    hooks install step, not a per-agent one). The *enabled_agent_types* set
    is used to log which actors are relevant; only CLI agents are relevant
    for beads integration — API agents have no local git identity.

    Returns the list of bd actor names that are active in this project.
    """
    if not (project_path / ".beads").exists():
        _logger.warning("bd_setup_skipped", reason="no_beads_dir")
        return []

    # Install git hooks (idempotent in bd).
    try:
        await bd("hooks", "install", cwd=project_path)
        _logger.info("bd_hooks_installed", project_path=str(project_path))
    except BdError as exc:
        _logger.warning("bd_hooks_install_failed", error=str(exc))
    else:
        # Route the hooks we just installed through the pinned bd (see
        # _pin_bd_in_hook_scripts) so commit-time hook runs can't silently
        # drift onto a different bd version than everything else uses.
        bd_binary = resolve_bd_binary()
        if bd_binary is not None:
            try:
                _pin_bd_in_hook_scripts(project_path, bd_binary)
            except OSError as exc:
                _logger.warning("bd_hook_pin_failed", error=str(exc))

    actors = [_BD_ACTOR_NAMES[at] for at in enabled_agent_types if at in _BD_ACTOR_NAMES]
    if actors:
        _logger.info("bd_agent_actors_configured", actors=actors)
    return actors


def run_beads_init(
    project_path: Path,
    enabled_agent_types: set[AgentType],
    *,
    assume_yes: bool = False,
) -> None:
    """Synchronous entry point called from `agentshore init` (Click context).

    Runs the full beads setup sequence:
      1. ensure_bd_installed  (synchronous check)
      2. bd_init_project      (async, run in a new event loop)
      3. reconcile_beads_schema (async, same loop) — schema-drift preflight
      4. bd_setup_for_agent_types (async, same loop)

    Any failure in step 1 propagates to the caller, as does a
    ``BeadsSchemaDriftError`` from step 3 (schema drift that nothing here
    could safely auto-heal must reach the caller so it can block ``agentshore
    init`` with the remediation command — see ``cli/commands/init.py`` —
    rather than silently proceeding against a store the rest of setup can't
    actually read). Step 2 and step 4 failures still only log warnings;
    a failed `bd init` or hooks install is a lesser problem than proceeding
    on top of unreadable schema drift, but still shouldn't block the rest of
    `agentshore init` on its own.

    *assume_yes* is threaded through to ``reconcile_beads_schema`` for an
    already-consented interactive caller (e.g. a wizard that confirmed with
    the user before calling this).
    """
    ensure_bd_installed()

    async def _run() -> None:
        await bd_init_project(project_path)
        await reconcile_beads_schema(project_path, assume_yes=assume_yes)
        await bd_setup_for_agent_types(project_path, enabled_agent_types)

    asyncio.run(_run())
