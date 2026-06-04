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

It is a thin collaborator (mirroring :class:`agentshore.core.github_syncer.GitHubSyncer`):
constructed in ``_OrchestratorBase.__init__`` and held on the orchestrator as
``_main_repo``. The pop-default-``None`` semantics of the pre-play handshake are
preserved byte-for-byte — they are load-bearing for the never-snapshotted and
double-pop paths.
"""

from __future__ import annotations


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

    # ------------------------------------------------------------------
    # Pre-play handshake
    # ------------------------------------------------------------------

    def record_pre_play_branch(self, dispatch_id: str, ref: str | None) -> None:
        self._pre_play_branches[dispatch_id] = ref

    def pop_pre_play_branch(self, dispatch_id: str) -> str | None:
        """Pop the recorded pre-play ref (default ``None`` if never recorded)."""
        return self._pre_play_branches.pop(dispatch_id, None)
