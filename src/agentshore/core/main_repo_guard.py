"""Main-repo branch guard: default branch + pre-play handshake + dispatch latch.

``MainRepoGuard`` owns the orchestrator state behind the desktop-kqo5 main-repo
branch-mutation guard:

* ``default_branch`` — the default branch resolved from ``origin/HEAD`` at
  session start (``phases._phase_git_safety_sweep``) and refreshed on SIGHUP
  (``lifecycle._reload_config``); read by the completion-time mutation check.
* the per-dispatch **pre-play handshake**: ``_dispatch_play`` records the
  symbolic ref captured before launching a play, keyed by ``dispatch_id``;
  ``_process_completion`` pops it (default ``None``) to detect a branch the
  play moved out from under the main checkout.
* ``dispatch_paused`` — a latch flipped True when an auto-restore fails
  (``main_repo_auto_restore_failed``) and cleared only by a successful
  RECONCILE_STATE. While latched, ``_dispatch_play`` refuses everything but
  END_AGENT / RECONCILE_STATE, and the idle-with-work watchdog auto-stops.
* the ``dirty_trunk`` wedge counter (#330) — consecutive ``merge_pr``
  failures blocked by the same set of root-level untracked paths that a
  deterministic reclaim sweep correctly leaves alone (e.g. real user WIP).
  At/above threshold ``is_trunk_wedged()`` becomes True, which only ever
  UNMASKS END_SESSION for the PPO's consideration (see
  ``rl/eligibility.py``) — the guard itself never forces a session action.
  ``CompletionProcessor._handle_merge_pr_outcome`` layers a separate,
  deliberate escalation on top once wedged (``_escalate_trunk_wedge`` in
  ``core/mixins/completion.py``): it force-quarantines the offending
  path(s) via ``trunk_artifacts.force_quarantine_wedge_paths`` and emits a
  ``trunk_wedge_needs_human`` signal, so a persistently-wedging file has a
  resolution path beyond only the give-up-and-end-session option above.

It is a thin collaborator (mirroring :class:`agentshore.core.github_syncer.GitHubSyncer`):
constructed in ``_OrchestratorBase.__init__`` and held on the orchestrator as
``_main_repo``. The pop-default-``None`` semantics of the pre-play handshake are
preserved byte-for-byte — they are load-bearing for the never-snapshotted and
double-pop paths.
"""

from __future__ import annotations

# Consecutive same-cause merge_pr dirty_trunk failures before the trunk is
# considered wedged. A new cause (different blocking path(s)) resets the
# streak to 1; a successful merge_pr clears it entirely.
_DIRTY_TRUNK_WEDGE_THRESHOLD = 3


class MainRepoGuard:
    """Owns the default branch, the pre-play ref handshake, and the dispatch latch."""

    def __init__(self, *, default_branch: str = "main") -> None:
        # Default branch resolved from origin/HEAD at session start; refreshed
        # on SIGHUP. Defaults to "main" until the session-start sweeper runs.
        self.default_branch = default_branch
        # Per-dispatch pre-play symbolic ref shadow, keyed by dispatch_id.
        # Populated in _dispatch_play, consumed (popped) in _process_completion;
        # entries are popped on consumption so the dict never grows past the
        # in-flight set.
        self._pre_play_branches: dict[str, str | None] = {}
        # Latched True on main_repo_auto_restore_failed; _dispatch_play consults
        # it before launching a task. Cleared by a successful RECONCILE_STATE.
        self.dispatch_paused = False
        # dirty_trunk wedge counter (#330): keyed on the sorted, joined set of
        # blocking root-level untracked paths from a live git-status read. A
        # new key (different blocking file(s)) resets the count to 1; a
        # successful merge_pr clears it entirely (see clear_dirty_trunk_failures).
        self._dirty_trunk_failure_key: str | None = None
        self._dirty_trunk_failure_count: int = 0

    # ------------------------------------------------------------------
    # Pre-play handshake
    # ------------------------------------------------------------------

    def record_pre_play_branch(self, dispatch_id: str, ref: str | None) -> None:
        self._pre_play_branches[dispatch_id] = ref

    def pop_pre_play_branch(self, dispatch_id: str) -> str | None:
        """Pop the recorded pre-play ref (default ``None`` if never recorded)."""
        return self._pre_play_branches.pop(dispatch_id, None)

    # ------------------------------------------------------------------
    # dirty_trunk wedge counter (#330)
    # ------------------------------------------------------------------

    def record_dirty_trunk_failure(self, key: str) -> None:
        """Record a merge_pr ``dirty_trunk`` failure blocked by root-untracked path(s) ``key``."""
        if key != self._dirty_trunk_failure_key:
            self._dirty_trunk_failure_key = key
            self._dirty_trunk_failure_count = 0
        self._dirty_trunk_failure_count += 1

    def clear_dirty_trunk_failures(self) -> None:
        """Clear the counter — called on any successful merge_pr."""
        self._dirty_trunk_failure_key = None
        self._dirty_trunk_failure_count = 0

    def is_trunk_wedged(self) -> bool:
        """True once the same-cause failure streak reaches the wedge threshold."""
        return self._dirty_trunk_failure_count >= _DIRTY_TRUNK_WEDGE_THRESHOLD
