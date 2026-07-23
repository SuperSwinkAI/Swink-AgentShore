"""Bootstrap phase functions for :class:`agentshore.core.Orchestrator`.

Each ``_phase_*`` callable runs one slice of the bootstrap pipeline and is
unit-tested in isolation. They are free functions (not methods) so the test
suite can substitute mocks for individual phases via ``patch`` while the
``bootstrap()`` classmethod orchestrates the ordering.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from agentshore.agents.manager import AgentManager
from agentshore.agents.model_tiers import enabled_model_tiers
from agentshore.core.git_safety import (
    commit_gitignore_if_dirty,
    current_head_ref,
    ensure_gitignore_entries,
    ensure_ssh_signing_key_loaded,
    find_path_escape_siblings,
    resolve_default_branch,
    restore_default_branch,
    ssh_signing_setup_hint,
    untrack_ignored_entries,
)
from agentshore.core.helpers import (
    _compute_config_hash,
    _logger,
    _ppo_selector_cls,
    _step,
)
from agentshore.data.store import DataStore, SessionRecord
from agentshore.github.labels import AGENTSHORE_WORKFLOW_LABELS
from agentshore.paths import GLOBAL_CONFIG_DIR as _GLOBAL_CONFIG_DIR
from agentshore.paths import GLOBAL_WEIGHTS_DIR as _GLOBAL_WEIGHTS_DIR
from agentshore.paths import project_db_path, project_dir, project_weights_dir
from agentshore.plays.base import PlayParams
from agentshore.plays.executor import PlayExecutor
from agentshore.plays.override import OverrideEntry, OverrideKind
from agentshore.plays.registry import build_default_registry
from agentshore.plays.resolver import ParameterResolver
from agentshore.rl.mask_reason import MaskClassification
from agentshore.state import AgentType, PlayType
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from agentshore.beads import ProjectGraph
    from agentshore.config import RuntimeConfig
    from agentshore.config.models import PolicyMode
    from agentshore.core.orchestrator import Orchestrator
    from agentshore.data.store import GitHubIssueRecord
    from agentshore.github.adapter import GitHubAdapter
    from agentshore.plays.registry import PlayRegistry
    from agentshore.rl.selector import PPOSelector
    from agentshore.state import StateProvider


GITHUB_ISSUE_FETCH_LIMIT = 200
GITHUB_PR_FETCH_LIMIT = 50

# Colors matched to dashboard agent brand (dashboard/src/characters/types.ts AGENT_COLORS).
_AUTHOR_LABEL_COLORS: dict[str, str] = {
    "claude_code": "E07B39",
    "codex": "F4D44D",
    "grok": "14B8A6",
    "antigravity": "4285F4",
}
_AUTHOR_LABEL_DEFAULT_COLOR = "cccccc"
_AGENTSHORE_SYSTEM_LABELS: tuple[tuple[str, str], ...] = AGENTSHORE_WORKFLOW_LABELS


async def _phase_init_datastore(repo_root: Path) -> DataStore:
    """Create ``.agentshore/`` and initialize the SQLite store."""
    async with _step("init_datastore"):
        db_dir = project_dir(repo_root)
        db_dir.mkdir(exist_ok=True)
        db_path = project_db_path(repo_root)
        # desktop-jc7p: swap in latest intact snapshot if main DB is corrupt
        # (corrupt file kept as agentshore.db.corrupt.<ts>); no-op when DB is fine.
        from agentshore.data.integrity import restore_from_snapshot_ring

        restore_from_snapshot_ring(db_path, db_dir)
        # Test seam: tests patch ``agentshore.core.phases.DataStore``.
        store: DataStore = DataStore(db_path)
        await store.initialize()
        return store


async def _phase_reset_session_scoped_tables(store: DataStore) -> None:
    """Truncate ephemeral tables so each session starts from a clean slate.

    Repo, GitHub, and beads are the source of truth for PR/issue/project graph state.
    Stale rows from prior sessions (especially pull_requests.author_agent_id
    stamps) cause code-review anti-confirmation dead-locks. The GH cache
    refresh in _phase_fetch_github repopulates these tables from live data.
    """
    async with _step("reset_session_scoped_tables"):
        await store.reset_session_scoped_tables()


async def _phase_init_executor(
    *,
    cfg: RuntimeConfig,
    repo_root: Path,
    sid: str,
    store: DataStore,
    provider: StateProvider,
) -> tuple[AgentManager, GitHubAdapter, PlayExecutor, PlayRegistry]:
    """Wire the manager, GitHub adapter, registry, resolver, and executor.

    Returns ``(manager, gh, executor, registry)``.
    """
    # Test seam: these symbols are patched on ``agentshore.core.phases``.
    async with _step("init_manager"):
        manager = AgentManager(
            session_id=sid,
            store=store,
            cfg=cfg,
            working_dir=repo_root,
            on_subprocess_spawned=provider.on_agent_subprocess_spawned,
            on_subprocess_exited=provider.on_agent_subprocess_exited,
        )

    async with _step("init_github"):
        from agentshore.github.adapter import GitHubAdapter

        gh = GitHubAdapter(store=store, session_id=sid, cfg=cfg)

    async with _step("init_executor"):
        registry = build_default_registry(cfg)
        resolver = ParameterResolver(
            store=store, manager=manager, cfg=cfg, github=gh, project_path=repo_root
        )
        executor = PlayExecutor(
            registry=registry,
            resolver=resolver,
            store=store,
            manager=manager,
            cfg=cfg,
            project_path=repo_root,
            session_id=sid,
            state_provider=provider,
            github=gh,
        )

    return manager, gh, executor, registry


async def _phase_init_metrics(
    *, orch: Orchestrator, cfg: RuntimeConfig, store: DataStore, sid: str, repo_root: Path
) -> None:
    """Wire the RL ``MetricsEngine`` and policy/config-version metadata."""
    async with _step("init_metrics"):
        from agentshore.rl.metrics import MetricsEngine

        orch._runtime.metrics = MetricsEngine(
            store=store,
            session_id=sid,
            repo_root=repo_root,
            learnings_file=cfg.learnings.file,
            stagnation_warn_after=cfg.rl.stagnation.warn_after,
            velocity_provider=orch._velocity.compute_rolling_velocity,
            executor_skip_rate_provider=orch._velocity.executor_skip_rate_recent_50,
        )
        orch._config_hash = _compute_config_hash(cfg)
        orch._runtime.policy_version = f"ppo-v1-{orch._config_hash[:8]}"


def _phase_cleanup_stale_weights(repo_root: Path) -> None:
    """Prune local + global PPO weight directories of stale checkpoints."""
    from agentshore.rl.selector import (
        _archive_old_canonicals,
        _prune_local_checkpoints,
        cleanup_stale_canonical_weights,
    )

    weights_dir = project_weights_dir(repo_root)
    weights_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_canonical_weights(weights_dir)
    _prune_local_checkpoints(weights_dir)
    _archive_old_canonicals(weights_dir)
    _GLOBAL_WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_stale_canonical_weights(_GLOBAL_WEIGHTS_DIR)
    _prune_local_checkpoints(_GLOBAL_WEIGHTS_DIR)
    _archive_old_canonicals(_GLOBAL_WEIGHTS_DIR)


def _resolve_policy_path(cfg: RuntimeConfig, policy_path: Path | None) -> Path | None:
    """Pick the first existing policy weight file from the candidate sources.

    Resolution order:
      1. explicit ``policy_path`` argument
      2. ``cfg.rl.policy_path``
      3. ``~/.config/swink/agentshore/weights/<canonical_weights_filename()>`` (global user)
      4. bundled ``agentshore.data/bootstrap_policy.pt``
    """
    pp = policy_path or (Path(cfg.rl.policy_path) if cfg.rl.policy_path else None)
    if pp is None or not pp.exists():
        from agentshore.rl.checkpoint_store import canonical_weights_filename

        global_policy = _GLOBAL_WEIGHTS_DIR / canonical_weights_filename()
        if global_policy.exists():
            pp = global_policy
    if pp is None or not pp.exists():
        import importlib.resources

        bootstrap = importlib.resources.files("agentshore.data") / "bootstrap_policy.pt"
        if bootstrap.is_file():
            pp = Path(str(bootstrap))
    return pp


def _resolve_seed_path(cfg: RuntimeConfig, seed_path: Path | None, repo_root: Path) -> Path | None:
    """Resolve the effective seed input for bootstrap.

    Resolution order (mirrors ``_resolve_policy_path``):
      1. explicit ``seed_path`` — CLI ``--seed`` or the sidecar
         ``session.start`` ``seed_input_path`` param (a one-off override).
      2. the first ``cfg.intake.seed_paths`` entry, resolved relative to
         ``repo_root``.

    A transient ``seed_path`` always wins. A configured seed that is missing or
    unusable degrades to ``None`` (open-start) with a warning — never a crash —
    so a stale ``agentshore.yaml`` seed path can't wedge startup.
    """
    if seed_path is not None:
        return seed_path
    if not cfg.intake.seed_paths:
        return None

    from agentshore.seed_input import SeedInputError, resolve_seed_input

    candidate = Path(cfg.intake.seed_paths[0]).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    try:
        resolved, _kind = resolve_seed_input(str(candidate), repo_root)
    except SeedInputError as exc:
        _logger.warning("seed_config_unusable", seed=cfg.intake.seed_paths[0], error=str(exc))
        return None
    _logger.info("seed_input_from_config", seed=str(resolved), source="intake.seed_paths")
    return resolved


async def _phase_init_ppo_selector(
    *,
    orch: Orchestrator,
    cfg: RuntimeConfig,
    executor: PlayExecutor,
    registry: PlayRegistry,
    policy_path: Path | None,
    policy_mode: PolicyMode,
) -> None:
    """Load the PPO selector from disk or fall back to a cold-start instance."""
    async with _step("init_ppo_selector"):
        from agentshore.rl.config_head import build_config_index

        if orch._runtime.metrics is None:
            msg = "Metrics engine must be initialized before PPO selector bootstrap"
            raise RuntimeError(msg)

        config_index = build_config_index(cfg)
        pp = _resolve_policy_path(cfg, policy_path)

        # #247: pre-warm the cold ~1-3s torch import OFF the event loop. Run on
        # the loop it freezes Textual's compositor; priming the module cache in a
        # thread makes the subsequent _ppo_selector_cls() a cache hit.
        import importlib

        await asyncio.to_thread(importlib.import_module, "agentshore.rl.selector")
        ppo_cls = _ppo_selector_cls()
        ppo: PPOSelector | None = None
        if pp is not None and pp.exists():
            try:
                ppo = await ppo_cls.load(
                    weights_path=pp,
                    resolver=executor._resolver,
                    registry=registry,
                    metrics=orch._runtime.metrics,
                    cfg=cfg.rl,
                    policy_mode=policy_mode,
                    policy_version=orch._runtime.policy_version,
                    config_hash=orch._config_hash,
                    orchestrator_cfg=cfg,
                    config_index=config_index,
                )
            except (FileNotFoundError, RuntimeError, ValueError, KeyError, OSError) as exc:
                _logger.warning("ppo_load_failed", error=str(exc))

        if ppo is None:
            ppo = ppo_cls.from_cold_start(
                resolver=executor._resolver,
                registry=registry,
                metrics=orch._runtime.metrics,
                cfg=cfg.rl,
                policy_mode=policy_mode,
                policy_version=orch._runtime.policy_version,
                config_hash=orch._config_hash,
                orchestrator_cfg=cfg,
                config_index=config_index,
            )

        orch._runtime.selector = ppo

        # Wire the RL experience recorder now selector/metrics/versions are final.
        # Completion no-ops the RL tail when this stays None (non-PPO / headless).
        from agentshore.core.concurrency_log import ConcurrencyLog
        from agentshore.core.experience_recorder import ExperienceRecorder
        from agentshore.session_path import session_dir

        orch._runtime.experience_recorder = ExperienceRecorder(
            store=orch._store,
            metrics=orch._runtime.metrics,
            selector=ppo,
            cfg=cfg,
            host=orch,
            velocity=orch._velocity,
            concurrency_log=ConcurrencyLog(session_dir(orch._repo_root), orch._session_id),
        )

        # Single autonomous-stop signal: drain after N consecutive ticks with no
        # agent dispatch, all agents idle, and no beads/GitHub graph change.
        from agentshore.core.progress_monitor import ForwardProgressMonitor

        orch._runtime.progress_monitor = ForwardProgressMonitor()


async def _phase_create_session_row(
    *, store: DataStore, sid: str, repo_root: Path, seed_path: Path | None
) -> None:
    """Insert the session row early so FK-referencing inserts work later."""
    async with _step("create_session"):
        await store.create_session(
            SessionRecord(
                session_id=sid,
                project_path=str(repo_root),
                started_at=now_iso(),
                status="running",
                seed_path=str(seed_path or ""),
            )
        )


async def _phase_init_worktree_manager(
    *, orch: Orchestrator, cfg: RuntimeConfig, store: DataStore, sid: str, repo_root: Path
) -> None:
    """Construct the ``WorktreeManager`` and attach it as ``orch._runtime.worktrees``.

    The manager owns the worktree lifecycle for the session — A2's dispatch
    wiring reads ``orch._runtime.worktrees`` (or the AgentManager-held reference)
    to allocate per-play worktrees, and the reaper hooks (session-start sweep,
    PR-close TTL) call ``reap_session_start`` / ``reap_closed_prs`` here.
    """
    async with _step("init_worktree_manager"):
        from agentshore.agents.worktree import WorktreeManager, default_worktree_root

        worktree_root = default_worktree_root(repo_root, cfg)
        worktree_root.mkdir(parents=True, exist_ok=True)
        orch._runtime.worktrees = WorktreeManager(
            session_id=sid,
            store=store,
            main_repo=repo_root,
            worktree_root=worktree_root,
            cfg=cfg,
        )
        _logger.info(
            "worktree_manager_initialized",
            session_id=sid,
            worktree_root=str(worktree_root),
        )


async def _phase_session_start_worktree_sweep(*, orch: Orchestrator, sid: str) -> None:
    """Reap leftover worktrees from prior sessions before any dispatch starts.

    Any row in ``worktrees`` with ``session_id != current`` and status in
    ``('active','reaping')`` gets ``git worktree remove --force`` plus a
    transition to ``reaped``. Safe to run with no orphans (no-op).

    Errors here are logged and swallowed — a transient SQLite or filesystem
    fault during a bootstrap sweep must not stop the session from starting.
    """
    if orch._runtime.worktrees is None:
        return
    async with _step("session_start_worktree_sweep"):
        try:
            report = await orch._runtime.worktrees.reap_session_start()
        except Exception as exc:
            _logger.warning(
                "session_start_worktree_sweep_failed",
                session_id=sid,
                error=str(exc),
            )
            return
        _logger.info(
            "session_start_worktree_sweep",
            session_id=sid,
            reaped=len(report.removed),
            failed=len(report.failed),
            git_orphans_removed=len(report.git_orphans_removed),
        )


async def _phase_session_start_dirty_baseline(*, repo_root: Path, sid: str) -> None:
    """Snapshot pre-session dirty trunk state to ``.agentshore/session_start_dirty.json``.

    Captures the trunk's modified-files state before any play dispatches so
    RECONCILE_STATE has authoritative pre-session evidence even if the DB
    or logs are lost (e.g. when the prior session crashed and the recovery
    swapped in a different DB). Survives DB corruption because the sidecar
    lives outside ``agentshore.db``.

    Errors are logged and swallowed — a failed snapshot only degrades
    RECONCILE_STATE to pre-sidecar log-scan behavior, never blocks the
    session from starting.
    """
    from datetime import datetime

    from agentshore.core.wedge_signals import write_session_start_dirty_baseline

    async with _step("session_start_dirty_baseline"):
        try:
            now_utc = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            dest = write_session_start_dirty_baseline(
                repo_root, session_id=sid, session_start_utc=now_utc
            )
        except Exception as exc:  # noqa: BLE001 — diagnostic is best-effort
            _logger.warning(
                "session_start_dirty_baseline_failed",
                session_id=sid,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return
        if dest is None:
            _logger.debug(
                "session_start_dirty_baseline_skipped",
                session_id=sid,
                reason="not a git repo or .agentshore missing",
            )
            return
        _logger.info(
            "session_start_dirty_baseline_written",
            session_id=sid,
            path=str(dest),
        )


async def _phase_session_start_trunk_artifacts(
    *,
    store: DataStore,
    cfg: RuntimeConfig,
    repo_root: Path,
    sid: str,
) -> None:
    """Reclaim untracked root artifacts orphaned by prior trunk-scoped plays.

    The per-play reclaim hook (``SkillBackedPlay``) cleans up a trunk-scoped
    play's debris at *normal* completion, but a play killed mid-flight never
    reaches its post-snapshot — its leftover root file lingers and wedges
    ``merge_pr`` / ``reconcile_state`` (#164). This bootstrap sweep closes that
    gap deterministically: each current untracked root file is attributed to the
    closed trunk-scoped play whose execution window brackets the file's mtime
    (via ``plays`` rows across all sessions), then quarantined under
    ``.agentshore/reclaimed/<play_id>/``. Files older than every trunk window
    (e.g. pre-session user WIP) are left untouched. Also TTL-reaps the
    quarantine dir. Errors are logged and swallowed — never blocks session start.
    """
    from agentshore.core.trunk_artifacts import (
        TRUNK_SCOPED_PLAY_TYPES,
        PlayWindow,
        reap_quarantine,
        sweep_and_reclaim_orphans,
    )
    from agentshore.utils import iso_to_epoch

    async with _step("session_start_trunk_artifacts"):
        try:
            rows = await store.list_trunk_play_windows(
                play_types=[pt.value for pt in TRUNK_SCOPED_PLAY_TYPES]
            )
            owner_windows: list[PlayWindow] = []
            for play_id, started_at, ended_at in rows:
                started = iso_to_epoch(started_at)
                if started is None:
                    continue
                owner_windows.append(
                    PlayWindow(play_id=play_id, started_at=started, ended_at=iso_to_epoch(ended_at))
                )
            # No active plays exist at bootstrap (dispatch not open), so a prior
            # killed play (ended_at NULL) is an owner, not active.
            reclaimed_total = await sweep_and_reclaim_orphans(
                repo_root,
                store=store,
                session_id=sid,
                owner_windows=owner_windows,
                active_windows=[],
                status="reclaimed_sweep",
            )
            reaped = reap_quarantine(repo_root, ttl_seconds=cfg.worktrees.reap_ttl_seconds)
            _logger.info(
                "session_start_trunk_artifacts",
                session_id=sid,
                reclaimed=reclaimed_total,
                quarantine_reaped=reaped,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never block startup
            _logger.warning(
                "session_start_trunk_artifacts_failed",
                session_id=sid,
                error=str(exc),
                exc_type=type(exc).__name__,
            )


async def _phase_git_safety_sweep(
    *,
    orch: Orchestrator,
    repo_root: Path,
    sid: str,
) -> None:
    """Cache the default branch and sweep for poisoned main-repo state.

    Three responsibilities, all part of the desktop-kqo5 / desktop-4ugk
    safety net:

    1. Resolve the project's default branch once via
       ``git symbolic-ref refs/remotes/origin/HEAD`` and cache it on the
       orchestrator. SIGHUP reload re-runs the resolver.
    2. If the main repo's HEAD is not on the cached default branch (a
       prior session left it on ``agentshore/*`` or somewhere else), emit
       ``main_repo_branch_mutated`` with phase=session_start and
       auto-restore. Failure to restore emits
       ``main_repo_auto_restore_failed`` and surfaces to the operator.
    3. Sweep the project root's parent directory for sibling names
       containing backslash-space — the canonical desktop-4ugk part 3
       leak. Surface as ``project_root_escape_detected`` info events;
       never auto-delete (operator must intervene).
    """
    async with _step("git_safety_sweep"):
        added = await asyncio.to_thread(ensure_gitignore_entries, repo_root)
        # A .gitignore line is a no-op for already-committed paths; untrack them
        # so the ignore takes effect.
        untracked = await asyncio.to_thread(untrack_ignored_entries, repo_root)
        if added or untracked:
            _logger.info(
                "gitignore_entries_added",
                session_id=sid,
                project_path=str(repo_root),
                entries=added,
                untracked=untracked,
            )
            committed = await asyncio.to_thread(commit_gitignore_if_dirty, repo_root)
            _logger.info(
                "gitignore_committed" if committed else "gitignore_commit_skipped",
                session_id=sid,
                entries=added,
                untracked=untracked,
            )

        ssh_loaded, ssh_detail = await asyncio.to_thread(ensure_ssh_signing_key_loaded)
        if ssh_loaded:
            _logger.info("ssh_signing_key_loaded", session_id=sid, detail=ssh_detail)
        else:
            _logger.warning(
                "ssh_signing_key_not_loaded",
                session_id=sid,
                detail=ssh_detail,
                note=(
                    "merge_pr plays will fail with 'ssh-signing-key-not-loaded'. "
                    f"Run: {ssh_signing_setup_hint()}"
                ),
            )

        default_branch, assumed = await asyncio.to_thread(resolve_default_branch, repo_root)
        orch._main_repo.default_branch = default_branch
        if assumed:
            _logger.warning(
                "default_branch_assumed",
                session_id=sid,
                project_path=str(repo_root),
                default_branch=default_branch,
                reason=(
                    "git symbolic-ref refs/remotes/origin/HEAD did not return "
                    "a usable ref; falling back to 'main'. Configure "
                    "project.target_branch in agentshore.yaml to silence this warning."
                ),
            )
        else:
            _logger.info(
                "default_branch_resolved",
                session_id=sid,
                project_path=str(repo_root),
                default_branch=default_branch,
            )

        expected_ref = f"refs/heads/{default_branch}"
        current_ref = await asyncio.to_thread(current_head_ref, repo_root)
        if current_ref != expected_ref:
            _logger.warning(
                "main_repo_branch_mutated",
                session_id=sid,
                project_path=str(repo_root),
                phase="session_start",
                pre_play_branch=expected_ref,
                post_play_branch=current_ref,
                default_branch=default_branch,
            )
            restore = await asyncio.to_thread(restore_default_branch, repo_root, default_branch)
            if not restore.ok:
                _logger.error(
                    "main_repo_auto_restore_failed",
                    session_id=sid,
                    project_path=str(repo_root),
                    phase="session_start",
                    default_branch=default_branch,
                    surfaced_ref=current_ref,
                    reason=restore.stderr,
                )
            else:
                _logger.info(
                    "main_repo_branch_restored",
                    session_id=sid,
                    project_path=str(repo_root),
                    phase="session_start",
                    default_branch=default_branch,
                )

        escapes = await asyncio.to_thread(find_path_escape_siblings, repo_root)
        for escape in escapes:
            _logger.warning(
                "project_root_escape_detected",
                session_id=sid,
                project_path=str(repo_root),
                escape_path=str(escape),
                escape_name=escape.name,
                reason=(
                    "Sibling directory name contains a literal backslash-space "
                    "sequence, suggesting a quoting bug in a skill template. "
                    "Inspect and remove manually; AgentShore will not auto-delete."
                ),
            )


def _phase_install_skills(repo_root: Path) -> None:
    """Install bundled skills into the project; non-fatal on failure."""
    # _step is async; use a synchronous timing log to keep this phase sync.
    t0 = time.perf_counter()
    try:
        from agentshore.skills import install_skills

        install_skills(repo_root)
    except OSError as exc:
        _logger.warning("skill_install_failed", error=str(exc))
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        _logger.info("bootstrap_step", step="install_skills", elapsed_ms=round(elapsed_ms, 1))


async def _clear_session_scoped_bead_progress(
    *,
    repo_root: Path,
    sid: str,
    phase: str,
) -> int:
    """Best-effort cleanup for beads ``in_progress`` state at session boundaries."""
    from agentshore.beads import clear_in_progress_beads

    try:
        reset_count = await clear_in_progress_beads(repo_root)
    except Exception as exc:
        _logger.warning(
            "beads_in_progress_clear_failed",
            phase=phase,
            session_id=sid,
            project_path=str(repo_root),
            error=str(exc),
            exc_info=True,
        )
        return 0

    _logger.info(
        "beads_in_progress_cleared",
        phase=phase,
        session_id=sid,
        project_path=str(repo_root),
        count=reset_count,
    )

    if phase == "session_shutdown":
        # Push the just-cleaned local beads store to its remote (if any) so
        # session-end state survives a lost machine/disk. Single-writer by
        # construction (session shutdown is the sole beads writer at this
        # point), so upstream's concurrent-push corruption warning doesn't
        # apply. Best-effort: never raises, never blocks shutdown.
        from agentshore.beads.durability import push_beads_remote

        await push_beads_remote(repo_root)

    return reset_count


async def _phase_clear_beads_in_progress(*, repo_root: Path, sid: str) -> None:
    """Clear stale beads progress before the first session state snapshot."""
    async with _step("clear_beads_in_progress"):
        # Test seam: ``_clear_session_scoped_bead_progress`` is patched here.
        await _clear_session_scoped_bead_progress(
            repo_root=repo_root,
            sid=sid,
            phase="session_start",
        )


def _author_labels_for_config(cfg: RuntimeConfig, prefix: str) -> list[tuple[str, str]]:
    """Return (label_name, hex_color) pairs for every known AgentType.

    All supported platforms get labels bootstrapped regardless of which agents
    are enabled in the current config — labels are cheap, and missing labels
    cause silent failures when an agent type is later enabled mid-project.
    """
    _ = cfg  # retained for signature compatibility; no longer filters by enabled
    return [
        (
            f"{prefix}author:{agent_type.value}",
            _AUTHOR_LABEL_COLORS.get(agent_type.value, _AUTHOR_LABEL_DEFAULT_COLOR),
        )
        for agent_type in AgentType
    ]


async def _mirror_issues_to_beads(
    project_path: Path,
    issues: list[GitHubIssueRecord],
    *,
    graph: ProjectGraph | None = None,
) -> None:
    """Import open GitHub issues as beads tasks (idempotent via external_ref).

    Contract
    --------
    * Skips entirely when ``.beads/`` does not exist for the project.
    * Skips entirely when the beads graph has no epics yet — ``seed_project``
      creates the canonical epic→story→task hierarchy; importing orphan tasks
      before that hierarchy exists produces floating "beads-only" cards that
      duplicate what ``seed_project`` will create.  Once epics exist,
      ``groom_backlog`` Step 4b handles newly-opened issues instead.
    * L4 coupling: mirrors only issues that passed the adapter label filters
      (``issue_labels_include`` / ``_exclude`` in config).  Changing those
      filters between sessions will orphan previously-mirrored tasks — the
      beads task remains while the matching issue is no longer returned.
    """
    import agentshore.beads as _beads_mod  # deferred — beads is an optional dep

    beads_dir = project_path / ".beads"
    if not beads_dir.exists():
        return
    # No epics yet → seed_project owns first-time graph construction; mirroring
    # now would create orphan tasks with no parent epic/story.
    if graph is None or not graph.has_epics:
        _logger.debug(
            "beads_mirror_skipped_no_epics",
            reason="seed_project will build the canonical graph",
        )
        return
    for issue in issues:
        if issue.state != "open":
            continue
        ext_ref = f"gh-{issue.issue_number}"
        # Already tracked → skip (idempotent re-run).
        if any(t.external_ref == ext_ref for t in graph.tasks):
            continue
        line = json.dumps(
            {
                "title": issue.title,
                "type": "task",
                "external_ref": ext_ref,
            }
        )
        try:
            await _beads_mod.bd(
                "import",
                "--dedup",
                "-",
                cwd=project_path,
                stdin_data=(line + "\n").encode(),
            )
        except _beads_mod.BdError as exc:
            # Dolt's "nothing to commit" (exit 1) means the import fully
            # deduplicated — a converged no-op, not a failure.
            if "nothing to commit" in str(exc):
                _logger.debug(
                    "beads_mirror_issue_noop",
                    issue_number=issue.issue_number,
                    reason="already_deduplicated",
                )
            else:
                _logger.warning(
                    "beads_mirror_issue_failed",
                    issue_number=issue.issue_number,
                    error=str(exc),
                )


async def _phase_fetch_github(
    *, gh: GitHubAdapter, store: DataStore, sid: str, cfg: RuntimeConfig, repo_root: Path
) -> None:
    """Probe GitHub, cache open issues + open PRs."""
    async with _step("fetch_issues"):
        try:
            await gh.probe()
            if not gh.available:
                # Probe failed — open_issues stays empty until refresh recovers.
                # ERROR so operators distinguish "empty repo" from "gh unavailable".
                _logger.error(
                    "github_unavailable",
                    expected_issues_known=False,
                    reason="gh CLI probe failed at session start; open_issues will be empty",
                    session_id=sid,
                )
            else:
                # desktop-rla8: full sweep at startup (no ``since`` cursor yet),
                # then advance the cursor so later refreshes use cheap ``since=``.
                from agentshore.core.github_syncer import GitHubSyncer, sync_cursor_now

                syncer = GitHubSyncer(gh=gh, store=store, cfg=cfg, session_id=sid)
                startup_cutoff = sync_cursor_now()
                issues = await syncer.fetch_issues(state="open", since=None)
                if issues is None:
                    _logger.error(
                        "github_issues_fetch_failed",
                        expected_issues_known=False,
                        note="gh list_issues returned None at startup; "
                        "open_issues will be empty until refresh recovers",
                        session_id=sid,
                    )
                elif issues:
                    await syncer.cache_issues(issues, cursor=startup_cutoff)
                    _logger.info(
                        "github_issues_cached",
                        count=len(issues),
                        expected_issues_known=True,
                        session_id=sid,
                    )
                    from agentshore.beads import (
                        BeadsSchemaDriftError,
                        GraphReadError,
                    )
                    from agentshore.beads import (
                        load_graph as _load_graph,
                    )

                    try:
                        _startup_graph = await _load_graph(repo_root)
                    except BeadsSchemaDriftError as exc:
                        # Lower-stakes than the bootstrap path (this only skips
                        # mirroring newly-fetched GitHub issues into beads for
                        # this tick, it doesn't decide whether to reseed), but
                        # still worth a distinct event so schema drift shows up
                        # as its own signal rather than folded into the
                        # generic read-failure count.
                        _logger.warning(
                            "beads_schema_drift_skipping_mirror",
                            error=str(exc),
                            session_id=sid,
                        )
                        _startup_graph = None
                    except GraphReadError as exc:
                        _logger.warning(
                            "beads_graph_read_failed_skipping_mirror",
                            error=str(exc),
                            session_id=sid,
                        )
                        _startup_graph = None
                    await _mirror_issues_to_beads(
                        project_path=repo_root, issues=issues, graph=_startup_graph
                    )
                else:
                    # Empty result is success — set cursor to skip the full
                    # sweep next refresh.
                    await syncer.cache_issues([], cursor=startup_cutoff)
                    _logger.info(
                        "github_issues_fetched_empty",
                        expected_issues_known=True,
                        note="0 open issues on GitHub (healthy empty-repo state)",
                        session_id=sid,
                    )
                pull_requests = await syncer.fetch_trusted_open_pull_requests(
                    limit=GITHUB_PR_FETCH_LIMIT,
                    context="startup",
                )
                if pull_requests:
                    await store.cache_pull_requests(sid, pull_requests)
                    _logger.info(
                        "github_pull_requests_cached",
                        count=len(pull_requests),
                        session_id=sid,
                    )
                    branch_pr_map = {pr.branch: pr.pr_number for pr in pull_requests if pr.branch}
                    if branch_pr_map:
                        await store.rebuild_branch_activity(sid, branch_pr_map)
                _logger.info(
                    "session_start_prs_snapshotted",
                    count=len(pull_requests),
                    pr_numbers=sorted(pr.pr_number for pr in pull_requests),
                    session_id=sid,
                )
        except (FileNotFoundError, TimeoutError, OSError, aiosqlite.Error) as exc:
            _logger.error(
                "github_fetch_failed",
                expected_issues_known=False,
                error=str(exc),
                exc_info=True,
            )
        except Exception as exc:
            _logger.error(
                "github_fetch_failed",
                expected_issues_known=False,
                error=str(exc),
                exc_info=True,
            )
    return None


async def _phase_ensure_labels(*, gh: GitHubAdapter, cfg: RuntimeConfig) -> None:
    """Ensure required workflow + author labels exist in the GitHub repo.

    Split out from `_phase_fetch_github` so its elapsed_ms is attributed
    separately — on cold repos, label creation dominates bootstrap and
    rolling it into ``fetch_issues`` made that step misleadingly slow.

    Any failure is logged and swallowed — labels are best-effort and a
    transient gh error must not stop a session from starting (matches the
    defensive behaviour `_phase_fetch_github` has always had).
    """
    async with _step("ensure_labels"):
        if not gh.available:
            return
        try:
            prefix = cfg.intake.label_prefix
            required_labels = [
                *_AGENTSHORE_SYSTEM_LABELS,
                *_author_labels_for_config(cfg, prefix),
            ]
            if required_labels:
                await gh.ensure_labels(required_labels)
        except Exception as exc:
            _logger.warning(
                "ensure_labels_failed",
                error=f"{type(exc).__name__}: {exc}",
            )


async def _phase_load_learnings(*, cfg: RuntimeConfig, repo_root: Path) -> None:
    """Load, age, prune, and decay learnings; merge global-scope entries.

    Behaviour preserved verbatim from the original monolithic ``bootstrap``:
    - skipped entirely if ``cfg.learnings.enabled`` is False
    - any failure is logged at WARNING and swallowed
    - global-scope entries that don't collide with a local id are merged in
    """
    async with _step("load_learnings"):
        if not cfg.learnings.enabled:
            return
        try:
            from agentshore.learnings import Learning as _Learning
            from agentshore.learnings import consolidate, decay, load, prune, save_atomic

            learnings_path = repo_root / cfg.learnings.file
            entries = await asyncio.to_thread(load, learnings_path)
            # Age every entry by one session.
            entries = [
                _Learning(
                    id=e.id,
                    pattern=e.pattern,
                    confidence=e.confidence,
                    sessions_since_use=e.sessions_since_use + 1,
                    source_play_id=e.source_play_id,
                    last_reinforced_play_id=e.last_reinforced_play_id,
                    created_at=e.created_at,
                    scope=getattr(e, "scope", "project"),
                )
                for e in entries
            ]
            entries = decay(
                prune(entries, min_confidence=cfg.learnings.min_confidence),
                threshold_sessions=cfg.learnings.decay_after_sessions,
            )
            # Compact near-duplicate prose so the max_entries trim doesn't drop
            # distinct insights.
            entries = consolidate(
                entries, overlap_threshold=cfg.learnings.consolidate_overlap_threshold
            )
            await asyncio.to_thread(save_atomic, learnings_path, entries)
            # Merge in global-scope learnings not already present locally.
            global_learnings_path = _GLOBAL_CONFIG_DIR / "learnings.json"
            if global_learnings_path.exists():
                try:
                    global_entries = await asyncio.to_thread(load, global_learnings_path)
                    # Global-scope only; project entries win on id collision.
                    project_ids = {e.id for e in entries}
                    for ge in global_entries:
                        if getattr(ge, "scope", "project") == "global" and ge.id not in project_ids:
                            entries.append(ge)
                except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
                    _logger.warning("global_learnings_load_failed", error=str(exc))
            _logger.info("learnings_loaded", count=len(entries))
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            _logger.warning("learnings_load_failed", error=str(exc))


def _enqueue_instantiate(
    orch: Orchestrator,
    agent_type: AgentType,
    tier: str,
    *,
    wait_for_play_type: PlayType | None = None,
) -> None:
    """Queue one bootstrap INSTANTIATE_AGENT override. Shared by both recipes."""
    orch._overrides.put_nowait(
        OverrideEntry(
            play_type=PlayType.INSTANTIATE_AGENT,
            params=PlayParams(
                target_agent_type=agent_type.value,
                target_model_tier=tier,
                bypass_preconditions=True,
            ),
            kind=OverrideKind.BOOTSTRAP,
            enqueue_classification=MaskClassification.INDEFINITE_WAIT,
            wait_for_play_type=wait_for_play_type,
        )
    )


def _enqueue_groom(
    orch: Orchestrator,
    cfg: RuntimeConfig,
    *,
    wait_for_play_type: PlayType | None = None,
) -> None:
    """Queue one bootstrap GROOM_BACKLOG override, unless the user disabled it.

    GROOM_BACKLOG is user-disableable (preferences.yaml). The bootstrap
    override bypasses the action mask, so the mask-level USER_DISABLED
    suppression can't reach it — honor the preference here or a disabled
    groom is force-run at cold start. Shared by both recipes.
    """
    if PlayType.GROOM_BACKLOG.value in cfg.preferences.disabled_plays:
        _logger.info(
            "bootstrap_groom_skipped",
            reason="user_disabled",
            wait_for_play_type=wait_for_play_type.value if wait_for_play_type else None,
        )
        return
    orch._overrides.put_nowait(
        OverrideEntry(
            play_type=PlayType.GROOM_BACKLOG,
            params=PlayParams(bypass_preconditions=True),
            kind=OverrideKind.BOOTSTRAP,
            enqueue_classification=MaskClassification.INDEFINITE_WAIT,
            wait_for_play_type=wait_for_play_type,
        )
    )


def _first_enabled_for_tier(
    cfg: RuntimeConfig,
    tier: str,
    *,
    exclude: frozenset[AgentType] = frozenset(),
) -> AgentType | None:
    """First configured enabled agent type offering *tier*. Used by the seed recipe."""
    for agent_key, agent_cfg in cfg.agents.items():
        try:
            agent_type = AgentType(agent_key)
        except ValueError:
            continue
        if agent_type in exclude:
            continue
        if (
            agent_cfg is not None
            and not isinstance(agent_cfg, dict)
            and getattr(agent_cfg, "enabled", False)
            and tier in enabled_model_tiers(agent_type, agent_cfg)
        ):
            return agent_type
    return None


def _enqueue_full_fleet(orch: Orchestrator, cfg: RuntimeConfig) -> int:
    """Queue one INSTANTIATE_AGENT override per enabled ``(agent_type, tier)``.

    The open recipe has no seed / first-play to serialize trunk access
    around, so the whole configured fleet spawns from cold and the PPO
    drives against a fully-staffed start ("full open"). The config *is* the
    fleet definition — every enabled tier the user configured comes up.
    Returns the number of overrides queued.
    """
    count = 0
    for agent_key, agent_cfg in cfg.agents.items():
        try:
            agent_type = AgentType(agent_key)
        except ValueError:
            continue
        if (
            agent_cfg is None
            or isinstance(agent_cfg, dict)
            or not getattr(agent_cfg, "enabled", False)
        ):
            continue
        for tier in enabled_model_tiers(agent_type, agent_cfg):
            _enqueue_instantiate(orch, agent_type, tier)
            count += 1
    return count


def _queue_open_recipe(
    *,
    orch: Orchestrator,
    cfg: RuntimeConfig,
    open_issues_count: int,
    graph_read_failed: bool,
) -> None:
    """Open-start "full open" recipe: no seed input, graph already has epics.

    Spawns the full enabled fleet plus a grooming pass:
      1. INSTANTIATE_AGENT — one per enabled ``(agent_type, tier)`` (#11). The
         mask zeroes INSTANTIATE_AGENT for a zero-agent / no-work / non-terminal
         fleet, so the forced spawns break the catch-22. No large-only pin: the
         config is the fleet definition and the whole of it comes up from cold so
         cheaper tiers (which own the mechanical plays) are present immediately;
         the PPO owns all subsequent fleet composition.
      2. GROOM_BACKLOG — once the fleet is online, reconcile the beads↔GitHub
         graph (sync untracked GH issues, clear resolved blocks) so the PPO
         starts from a clean backlog. Queued directly behind INSTANTIATE_AGENT
         with **no** ``wait_for`` gate — exactly like SEED_PROJECT in the seed
         recipe. As the first agent-consumer it must claim the agent by queue
         position: ``_consume_override`` returns one play per tick and PPO only
         selects on a tick where it returns ``None``. A ``wait_for`` gate here
         would yield such a ``None`` tick while the agent sits idle, and PPO
         would free-select a play onto it before groom's gate lifts — the agent
         would be busy by the time groom dequeues and groom would be skipped for
         staffing. No gate ⇒ groom dispatches the next tick onto the freshly
         idle agent, before PPO ever gets a turn.

    Mask.py zeroes INSTANTIATE_AGENT in the "no agents + no work + not
    terminal" state, so a quiet zero-agent repo idle-deadlocks; these forced
    bootstrap overrides break that catch-22. No deterministic step here
    touches trunk (groom is a beads↔GitHub reconcile), so #569's
    trunk-exclusivity gating (used by the seed recipe) doesn't apply.

    ``graph_read_failed`` also routes here rather than the seed recipe (see
    ``_phase_queue_agent_instantiation``'s docstring) — spawning agents never
    touches the beads store, so it's the only safe move when we don't
    actually know whether the graph has epics. GROOM_BACKLOG may still fail
    cleanly against the same unreadable store; that's the expected,
    self-diagnosing outcome.
    """
    spawned = _enqueue_full_fleet(orch, cfg)
    _logger.info(
        "bootstrap_open_start",
        reason="graph_read_failed" if graph_read_failed else "no_seed_full_open",
        open_issues_count=open_issues_count,
        agents_spawned=spawned,
    )
    if spawned:
        # Reconcile beads↔GitHub once the fleet is online, before PPO takes
        # over. NO wait_for gate: as first agent-consumer, groom claims an
        # agent by queue position — a gate yields a None tick that lets PPO
        # free-select onto the idle agent first and starves groom.
        _enqueue_groom(orch, cfg)


def _queue_seed_recipe(
    *,
    orch: Orchestrator,
    cfg: RuntimeConfig,
    seed_path: Path | None,
    open_issues_count: int,
    graph_has_epics: bool,
) -> None:
    """Seed recipe: explicit seed input, or seedless because the graph has no epics yet.

    Routing the no-epic case here (rather than the open recipe) avoids the
    groom-against-empty-graph deadlock — SEED_PROJECT creates the epics groom
    needs.

      1. INSTANTIATE_AGENT — first configured enabled large-tier agent.
      2. SEED_PROJECT — runs on the large agent (against the seed input when
         present, else seedless); agent is BUSY so the idle-agent gate holds the
         remaining queue until the seed audit completes.
      3. INSTANTIATE_AGENT — first configured enabled medium-tier agent of a
         different type, giving the initial fleet cross-backend coverage.
      4. GROOM_BACKLOG — reconciles the freshly-seeded beads graph against
         GitHub before the PPO takes over; gated on SEED_PROJECT completing
         (same trunk-exclusivity gate as the medium spawn, #569).
    """
    first_play_type = PlayType.SEED_PROJECT
    _logger.info(
        "bootstrap_first_play_decided",
        play_type=first_play_type.value,
        reason="seed_input_provided" if seed_path is not None else "no_epics_needs_seed",
        open_issues_count=open_issues_count,
        seed_input_provided=seed_path is not None,
        graph_has_epics=graph_has_epics,
    )

    t0 = time.perf_counter()
    try:
        large_agent_type = _first_enabled_for_tier(cfg, "large")
        if large_agent_type is not None:
            _enqueue_instantiate(orch, large_agent_type, "large")

        first_play_params = PlayParams(
            seed_path=str(seed_path) if seed_path is not None else None,
            bypass_preconditions=True,
        )
        orch._overrides.put_nowait(
            OverrideEntry(
                play_type=first_play_type,
                params=first_play_params,
                kind=OverrideKind.BOOTSTRAP,
                enqueue_classification=MaskClassification.INDEFINITE_WAIT,
            )
        )

        medium_agent_type = (
            _first_enabled_for_tier(cfg, "medium", exclude=frozenset({large_agent_type}))
            if large_agent_type is not None
            else None
        )
        if medium_agent_type is not None:
            # #569: gate the medium spawn behind the first-play completing — both
            # touch trunk and need exclusive access.
            _enqueue_instantiate(
                orch, medium_agent_type, "medium", wait_for_play_type=first_play_type
            )
        # Groom the seeded graph once SEED_PROJECT completes — same trunk-
        # exclusivity gate (#569). Needs an agent, so requires the large spawn.
        if large_agent_type is not None:
            _enqueue_groom(orch, cfg, wait_for_play_type=first_play_type)
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        _logger.info(
            "bootstrap_step", step="queue_agent_instantiation", elapsed_ms=round(elapsed_ms, 1)
        )


def _phase_queue_agent_instantiation(
    *,
    orch: Orchestrator,
    cfg: RuntimeConfig,
    seed_path: Path | None,
    open_issues_count: int = 0,
    graph_has_epics: bool = True,
    graph_read_failed: bool = False,
) -> None:
    """Queue the bootstrap recipe: ``_queue_open_recipe`` or ``_queue_seed_recipe``.

    The **seed recipe** (``_queue_seed_recipe``) runs whenever a seed input was
    provided *or* the beads graph has no epics yet (``graph_has_epics`` is
    False). In the latter case SEED_PROJECT runs *seedless* — it bootstraps the
    graph from the repo + GitHub issues (its precondition carve-out makes it
    eligible exactly when the graph is empty). Routing the no-epic case there
    is what prevents the open-path deadlock: GROOM_BACKLOG against an
    epic-less graph has nothing to reconcile and fails, so we must create
    epics first. Otherwise the **open recipe** (``_queue_open_recipe``) runs.

    ``graph_read_failed`` distinguishes "the graph load itself failed" (e.g.
    beads schema drift — ``BeadsSchemaDriftError``) from "the graph loaded
    fine and is genuinely empty" (``graph_has_epics=False`` with a successful
    read). The two used to be indistinguishable at this call site — a caught
    ``GraphReadError`` and a truly epic-less graph both surfaced as
    ``graph_has_epics=False`` — which let a live session's seedless
    SEED_PROJECT re-run over a project whose real epics/tasks simply
    couldn't be read that tick. When the read failed, this function *never*
    routes to the seed recipe: it treats the graph like the open recipe would
    (spawn the fleet; skip nothing on the strength of a guess), because
    seeding on top of an unknown, possibly-populated graph is the one truly
    harmful thing this bootstrap step could do, while spawning the configured
    fleet cannot touch the beads store at all. Each agent's own first play
    then discovers and reports the same drift on its own via the normal
    ``BeadsSchemaDriftError``/precondition path — this function's job is only
    to not compound the failure by guessing "empty" and reseeding.

    All entries are queued with ``bypass_preconditions=True`` so the
    deterministic recipe is not stalled by the cooldown, warmup-floor, or
    first-play-completion gates that PPO selections still see.

    The GROOM_BACKLOG step in either recipe is skipped entirely when the user
    has disabled it via ``preferences.yaml`` — the bootstrap override bypasses
    the action mask, so the preference must be honored at enqueue time (see
    ``_enqueue_groom``).
    """
    if seed_path is None and (graph_has_epics or graph_read_failed):
        _queue_open_recipe(
            orch=orch,
            cfg=cfg,
            open_issues_count=open_issues_count,
            graph_read_failed=graph_read_failed,
        )
        return
    _queue_seed_recipe(
        orch=orch,
        cfg=cfg,
        seed_path=seed_path,
        open_issues_count=open_issues_count,
        graph_has_epics=graph_has_epics,
    )
