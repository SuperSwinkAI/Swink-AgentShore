"""Consecutive same-cause merge_pr ``dirty_trunk`` wedge detection + escalation (#330)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from agentshore.core.helpers import _logger
from agentshore.core.trunk_artifacts import force_quarantine_wedge_paths
from agentshore.core.wedge_signals import collect_dirty_trunk_paths
from agentshore.data.models import ExternalMutationRecord
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.core.main_repo_guard import MainRepoGuard
    from agentshore.data.store import DataStore
    from agentshore.state import PlayOutcome


class TrunkWedgeEscalator:
    """Tracks consecutive same-cause merge_pr ``dirty_trunk`` failures; resolves + surfaces a wedge.

    Extracted from ``CompletionProcessor`` — all behaviour is verbatim.
    Constructed inside ``CompletionProcessor.__init__`` from the already-
    injected deps; ``CompletionProcessor._handle_merge_pr_outcome`` delegates
    here via the same unbound-shim pattern
    ``IssueSyncer._mark_worktrees_stale_for_closed_prs`` uses
    (``TrunkWedgeEscalator.handle_merge_pr_outcome(self, ...)`` with the
    CompletionProcessor or a bare test stub as ``self``), so the pre-existing
    stub-harness tests — which bypass ``CompletionProcessor.__init__`` and set
    only ``_session_id``, ``_main_repo``, ``_repo_root``, and optionally
    ``_store`` — keep working unmodified. The internal
    ``escalate_trunk_wedge`` sub-call is likewise referenced via the class
    (``TrunkWedgeEscalator.escalate_trunk_wedge(self, ...)``) rather than
    ``self.escalate_trunk_wedge(...)``, since a bare stub never defines that
    method itself.
    """

    def __init__(
        self,
        *,
        store: DataStore,
        session_id: str,
        repo_root: Path,
        main_repo: MainRepoGuard,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._repo_root = repo_root
        self._main_repo = main_repo

    async def handle_merge_pr_outcome(self, outcome: PlayOutcome) -> None:
        """Track consecutive same-cause merge_pr ``dirty_trunk`` failures (#330).

        A successful merge_pr clears the counter. A ``dirty_trunk`` failure
        blocked by root-level untracked path(s) a deterministic reclaim sweep
        correctly leaves alone (real user WIP, or predates every known play
        window) records those paths as the failure's cause; once the same
        cause repeats past the guard's threshold, ``state.trunk_wedged``
        unmasks END_SESSION for the PPO (see ``rl/eligibility.py``) — this
        method never *forces* a session action, only feeds the counter. Once
        wedged, it additionally escalates (``escalate_trunk_wedge``):
        force-quarantining the offending path(s) and emitting a needs-human
        signal, so the wedge has a resolution path instead of only a give-up
        option.
        """
        if outcome.success:
            self._main_repo.clear_dirty_trunk_failures()
            return
        error_text = (outcome.error or "").lower()
        if "dirty_trunk" not in error_text:
            return
        entries = await asyncio.to_thread(collect_dirty_trunk_paths, self._repo_root)
        root_untracked = sorted(e.path for e in entries if e.status == "??" and "/" not in e.path)
        if not root_untracked:
            # Not this pathology (tracked collision or subdirectory debris) —
            # leave to other handling.
            return
        self._main_repo.record_dirty_trunk_failure("|".join(root_untracked))
        if self._main_repo.is_trunk_wedged():
            await TrunkWedgeEscalator.escalate_trunk_wedge(
                self, root_untracked, play_id=outcome.play_id
            )

    async def escalate_trunk_wedge(self, root_untracked: list[str], *, play_id: int | None) -> None:
        """Resolve + surface a wedged trunk once the same-cause streak hits threshold (#330).

        ``MainRepoGuard`` documents that it "never forces a session action" —
        ``is_trunk_wedged()`` only unmasks END_SESSION for the PPO to weigh.
        That principle is preserved here too: this method does not stop the
        session, block dispatch, or force any play. What it *does* do is act
        on strong evidence the guard itself can't see — three consecutive
        ``dirty_trunk`` failures blocked by the exact same root path(s) means
        a deterministic reclaim sweep's conservative "might be real user WIP"
        assumption has been falsified for this file. Reclaim normally requires
        mtime-window attribution to a closed trunk-scoped play
        (``attribute_orphan_artifacts``); an unattributable file never clears
        that bar and would otherwise wedge forever with no resolution path
        beyond an operator noticing the session ended.

        Two independent actions, both best-effort and non-blocking:

        1. Force-quarantine the recorded path(s) into
           ``.agentshore/reclaimed/wedge/`` (move, never delete) so ``git
           status`` goes clean and the next ``merge_pr`` attempt can succeed
           on its own — no operator action required for the common case.
        2. Emit a ``trunk_wedge_needs_human`` warning (the same log-event
           surface pattern as ``pr_manual_required`` / ``issue_needs_human``
           in ``terminal_park.py``) regardless of whether quarantine fully
           succeeded, so the operator always gets a clear, visible signal
           naming the exact path(s) that wedged the trunk.
        """
        quarantined = await asyncio.to_thread(
            force_quarantine_wedge_paths, self._repo_root, root_untracked
        )
        store = getattr(self, "_store", None)
        if store is not None:
            for rel in quarantined:
                try:
                    await store.record_external_mutation(
                        ExternalMutationRecord(
                            session_id=self._session_id,
                            play_id=play_id,
                            idempotency_key=f"wedge_quarantine:{self._session_id}:{rel}:{now_iso()}",
                            mutation_type="trunk_artifact_wedge_quarantine",
                            target=rel,
                            status="wedge_quarantined",
                            created_at=now_iso(),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — audit trail is best-effort
                    _logger.warning(
                        "trunk_wedge_quarantine_mutation_record_failed",
                        session_id=self._session_id,
                        path=rel,
                        error=str(exc),
                    )
        _logger.warning(
            "trunk_wedge_needs_human",
            session_id=self._session_id,
            blocking_paths=root_untracked,
            quarantined_paths=quarantined,
            unresolved_paths=sorted(set(root_untracked) - set(quarantined)),
        )
