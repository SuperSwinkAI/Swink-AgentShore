"""Central play-candidate discovery for state masks, diagnostics, and resolvers."""

from __future__ import annotations

import contextlib
import json
import random
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from agentshore.agents._selection import allowed_tiers_for
from agentshore.agents.capabilities import AGENT_CAPABILITIES
from agentshore.agents.model_tiers import DEFAULT_MODEL_TIER
from agentshore.agents.worktree import TRUNK_MUTATING_PLAYS
from agentshore.beads import BeadStatus, ready_tasks
from agentshore.github.labels import (
    BUG_LABELS,
    DEBUG_TRIGGER_LABELS,
    DISALLOWED_LABEL,
    ISSUE_PICKUP_SKIP_LABELS,
    MANUAL_REQUIRED_LABEL,
    NEEDS_HUMAN_LABEL,
    PLANNED_LABELS,
    PRIORITY_SCORES,
    ROOT_CAUSE_FOUND_LABEL,
)
from agentshore.github.pr_links import canonical_issue_numbers, issue_numbers_for_pr
from agentshore.github.trust import filter_trusted_pull_requests
from agentshore.identity_names import canonical_identity_name, same_identity
from agentshore.logging import get_logger
from agentshore.play_rules import (
    DESIGN_AUDIT_FRESHNESS_WINDOW_PLAYS,
    SEED_PROJECT_COOLDOWN_PLAYS,
    TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS,
    needs_review,
)
from agentshore.plays.base import PlayParams
from agentshore.pr_state import blocked_reasons
from agentshore.state import AgentStatus, PlayType, is_agent_circuit_broken

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentshore.config import RuntimeConfig
    from agentshore.data.models import PullRequestRecord
    from agentshore.data.store import DataStore
    from agentshore.github.adapter import GitHubAdapter
    from agentshore.state import AgentSnapshot, IssueSnapshot, OrchestratorState

_logger = get_logger(__name__)

# Synthetic resource key for trunk-scoped plays. Must match the constant
# in ``agentshore.plays.resolver`` — kept duplicated to avoid a circular
# import (resolver imports candidates already).
_TRUNK_RESOURCE_KEY = "trunk:main_repo"

# Backpressure: once the open-PR queue reaches this many PRs, ``issue_pickup`` is
# masked so the policy clears review/merge work before opening more PRs. Shared
# single source of truth — ``issue_pickup`` imports it for the mask, and
# ``build_candidate_plan`` derives ``pr_queue_human_blocked`` partly from
# ``MAX_OPEN_PRS - 1`` so the END_SESSION escape hatch stays coupled to the cap
# that creates the jam (a queue of human-blocked PRs at/near the cap cannot make
# progress); it also fires below the cap when every open PR is manual-required
# and no other actionable work remains.
MAX_OPEN_PRS = 10

_SIZE_RANK: dict[str, int] = {
    "size/S": 0,
    "size/M": 1,
    "size/L": 2,
    "size/XL": 3,
}
_NO_PRIORITY_SORT_KEY = 999


@dataclass(frozen=True, slots=True)
class PlayCandidate:
    """A concrete target candidate for a selected play type."""

    play_type: PlayType
    params: PlayParams
    resource_keys: tuple[str, ...]
    source: str
    sort_key: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WorkAvailability:
    """Counts that distinguish visible work from actionable AgentShore work."""

    tracked_issue_count: int
    github_open_issue_count: int
    workable_issue_count: int
    blocked_issue_count: int
    disallowed_issue_count: int
    untrusted_issue_count: int
    # Open PRs dropped this tick because their base branch != target_branch
    # (Piece C). Surfaced so the dashboard can render an "(N hidden)" badge.
    pull_requests_hidden_count: int
    covered_by_open_pr_count: int
    resolved_by_merged_pr_count: int
    in_flight_issue_count: int
    bead_in_progress_issue_count: int
    bead_blocked_issue_count: int
    ready_task_count: int
    beads_blocks_issue_pickup: bool
    untracked_gh_issue_count: int
    unlinked_ready_task_count: int
    backlog_sync_work_count: int
    planning_eligible_count: int
    implementation_eligible_count: int
    refinement_eligible_count: int
    debugging_eligible_count: int
    reviewable_pr_count: int
    mergeable_pr_count: int
    unblockable_pr_count: int
    actionable_pr_work_count: int
    # Count of open PRs parked behind MANUAL_REQUIRED_LABEL (human intervention
    # required). pr_queue_human_blocked flags the wedge so the END_SESSION gate
    # can offer a terminal play. It is True when this reaches MAX_OPEN_PRS - 1
    # (the open-PR backpressure cap is saturated with human-blocked PRs so no new
    # issue work can produce a mergeable PR) OR when *every* open PR is
    # manual-required and no other actionable work remains (the queue cannot drain
    # without a human even below the cap).
    manual_required_open_pr_count: int
    pr_queue_human_blocked: bool
    terminal_no_work: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PlayCandidatePlan:
    """State-only candidate plan shared by masks, logs, IPC, and core idle checks."""

    candidates_by_play_type: dict[PlayType, tuple[PlayCandidate, ...]]
    blocked_reasons_by_play_type: dict[PlayType, tuple[str, ...]]
    work_availability: WorkAvailability
    has_remaining_work: bool

    def candidates_for(self, play_type: PlayType) -> tuple[PlayCandidate, ...]:
        return self.candidates_by_play_type.get(play_type, ())


def pr_resource_keys(
    pr_number: int,
    issue_number: int | None = None,
    linked_issue_numbers: object = (),
) -> tuple[str, ...]:
    keys = [f"pr:{pr_number}"]
    raw_links: tuple[object, ...]
    if linked_issue_numbers is None:
        raw_links = ()
    elif isinstance(linked_issue_numbers, str):
        raw_links = (linked_issue_numbers,)
    elif isinstance(linked_issue_numbers, Iterable):
        raw_links = tuple(linked_issue_numbers)
    else:
        raw_links = (linked_issue_numbers,)
    for linked_issue_number in canonical_issue_numbers((issue_number, *raw_links)):
        keys.append(f"issue:{linked_issue_number}")
    return tuple(keys)


def pr_resource_keys_for_pr(pr: object) -> tuple[str, ...]:
    pr_number = getattr(pr, "pr_number", None)
    if isinstance(pr_number, bool) or not isinstance(pr_number, int):
        return ()
    return pr_resource_keys(
        pr_number,
        getattr(pr, "issue_number", None),
        issue_numbers_for_pr(pr),
    )


def issue_resource_keys(issue_number: int) -> tuple[str, ...]:
    return (f"issue:{issue_number}",)


def active_resource_keys(state: OrchestratorState) -> frozenset[str]:
    """Return canonical resource keys currently owned by in-flight work."""

    keys: set[str] = set()
    pr_to_issues = {
        pr.pr_number: issue_numbers_for_pr(pr)
        for pr in state.pull_requests
        if issue_numbers_for_pr(pr)
    }
    issue_to_prs: dict[int, set[int]] = {}
    for pr in state.pull_requests:
        for issue_number in issue_numbers_for_pr(pr):
            issue_to_prs.setdefault(issue_number, set()).add(pr.pr_number)

    def add_issue(issue_number: int | None) -> None:
        if issue_number is None:
            return
        keys.add(f"issue:{issue_number}")
        for pr_number in issue_to_prs.get(issue_number, ()):
            keys.add(f"pr:{pr_number}")

    def add_pr(pr_number: int | None) -> None:
        if pr_number is None:
            return
        keys.add(f"pr:{pr_number}")
        for issue_number in pr_to_issues.get(pr_number, ()):
            add_issue(issue_number)

    for issue_number in state.in_flight_issues:
        add_issue(issue_number)

    active_play = state.active_play
    if active_play is not None:
        add_issue(active_play.issue_number)
        add_pr(active_play.pr_number)

    for agent in state.agents:
        if agent.current_play_type is None:
            continue
        add_issue(agent.current_play_issue_number)
        add_pr(agent.current_play_pr_number)
        # Only trunk-*mutating* plays mark trunk busy. A running read-only
        # trunk play (run_qa, design_audit, …) must not mask merge_pr (#17).
        if agent.current_play_type in TRUNK_MUTATING_PLAYS:
            keys.add(_TRUNK_RESOURCE_KEY)

    # in_flight_plays carries play types that lack agent context (e.g. a
    # play dispatched before the agent snapshot updates). Still need to
    # honor their trunk lock — but again only for trunk-mutating plays.
    for play_type in state.in_flight_plays:
        if play_type in TRUNK_MUTATING_PLAYS:
            keys.add(_TRUNK_RESOURCE_KEY)
            break

    return frozenset(keys)


def resource_conflict_reason(
    resource_keys: tuple[str, ...],
    active_keys: frozenset[str],
) -> str | None:
    """Return a stable blocked reason when a candidate overlaps active work."""

    conflicts = sorted(set(resource_keys) & set(active_keys))
    if not conflicts:
        return None
    return f"resource already in flight: {', '.join(conflicts)}"


def issue_pickup_sort_key(issue: IssueSnapshot) -> tuple[int, int, int, int]:
    """Sort tuple: bug first, then priority, size, and issue number."""

    is_bug = 0 if any(lbl in BUG_LABELS for lbl in issue.labels) else 1
    if issue.priority is not None:
        priority = issue.priority
    else:
        priority = next(
            (PRIORITY_SCORES[lbl] for lbl in issue.labels if lbl in PRIORITY_SCORES),
            _NO_PRIORITY_SORT_KEY,
        )
    size = next(
        (_SIZE_RANK[lbl] for lbl in issue.labels if lbl in _SIZE_RANK),
        _NO_PRIORITY_SORT_KEY,
    )
    return (is_bug, priority, size, issue.issue_number)


def _pr_blocked_reasons(pr: object, *, labels: list[str] | None = None) -> list[str]:
    """Gather the eight PR fields once and return the canonical blocked reasons.

    Shared by :func:`pr_merge_ready` and :func:`pr_unblockable` so the two
    predicates can never disagree on the underlying field reads. ``labels`` may
    be passed when the caller has already computed them to avoid a second
    ``_labels(pr)`` traversal.
    """
    return blocked_reasons(
        state=str(getattr(pr, "state", "")),
        labels=_labels(pr) if labels is None else labels,
        review_decision=_string_or_none(getattr(pr, "review_decision", None)),
        status_check_summary=_string_or_none(getattr(pr, "status_check_summary", None)),
        is_draft=_bool_or_none(getattr(pr, "is_draft", None)),
        mergeable=getattr(pr, "mergeable", None),
    )


def pr_merge_ready(pr: object, *, target_branch: str | None = None) -> bool:
    """Return True for the one canonical merge-pr readiness predicate.

    A PR is merge-ready only when GitHub reports it mergeable, an approval
    signal is present (GitHub APPROVED or an AgentShore code-review PASS at the
    current head_sha), and no blocking reason (changes_requested, blocked
    label, ci_failed, manual_required, merge_conflicts) is in effect. The
    blocking check shares :func:`_pr_blocked_reasons` with
    :func:`pr_unblockable` so the two predicates can never simultaneously be
    true for the same PR — without this, a stale AgentShore PASS verdict can
    keep a PR in the merge_ready set even after a human reviewer adds
    CHANGES_REQUESTED or a ``blocked`` label.

    When ``target_branch`` is provided, a PR whose ``base_ref`` is known and
    does NOT match it is refused — a deterministic backstop so ``merge_pr`` can
    never merge a PR opened against the wrong base (e.g. ``main`` instead of the
    configured ``integration``), independent of whether the authoring/merging
    agent honored the skill's base step. The create-side auto-correction
    (executor ``_wire_deferrals``) retargets such PRs to the target, after which
    they re-qualify here.
    """

    reasons = _pr_blocked_reasons(pr)
    if reasons and reasons != ["draft"]:
        return False

    base_ref = getattr(pr, "base_ref", None)
    if isinstance(base_ref, str) and base_ref.startswith("agentshore/"):
        return False
    # Deterministic base gate: refuse to merge a PR whose known base is not the
    # configured target branch. Pairs with the create-side auto-correction.
    if target_branch and isinstance(base_ref, str) and base_ref and base_ref != target_branch:
        return False

    review_decision = getattr(pr, "review_decision", None)
    last_review_status = getattr(pr, "last_review_status", None)
    last_reviewed_sha = getattr(pr, "last_reviewed_sha", None)
    head_sha = getattr(pr, "head_sha", None)
    # The AgentShore PASS-at-head branch is the autonomous approval path (no
    # human reviewer). It must never override a live human CHANGES_REQUESTED —
    # _pr_blocked_reasons already returns early on that, but guard explicitly so
    # the invariant is local and can't regress if the gate above changes (#344).
    return bool(
        getattr(pr, "mergeable", None) == "MERGEABLE"
        and review_decision != "CHANGES_REQUESTED"
        and (
            review_decision == "APPROVED"
            or (
                last_review_status == "PASS"
                and last_reviewed_sha is not None
                and head_sha is not None
                and last_reviewed_sha == head_sha
            )
        )
    )


def pr_review_needed(pr: object) -> bool:
    return bool(not getattr(pr, "is_draft", False) and needs_review(pr))


def pr_reviewable(pr: object) -> bool:
    """code_review is actionable only when the PR needs review AND is not parked
    for human intervention.

    Like :func:`pr_unblockable` and :func:`pr_merge_ready` (which exclude
    ``MANUAL_REQUIRED_LABEL`` via ``_pr_blocked_reasons``), this excludes a
    manual-required PR, so all three actionable-PR predicates agree: a
    manual-required PR is never agent-actionable. Without this, an unreviewed
    manual-required PR leaks into the reviewable set, keeping
    ``has_actionable_work`` (and thus ``terminal_no_work``) wrong and pinning
    END_SESSION masked forever.
    """
    if MANUAL_REQUIRED_LABEL in _labels(pr):
        return False
    return pr_review_needed(pr)


def pr_unblockable(pr: object) -> bool:
    labels = _labels(pr)
    if MANUAL_REQUIRED_LABEL in labels:
        return False
    if getattr(pr, "blocked", False):
        return True
    if getattr(pr, "mergeable", None) == "CONFLICTING":
        return True
    reasons = _pr_blocked_reasons(pr, labels=labels)
    return bool(reasons and reasons != ["draft"])


# ---------------------------------------------------------------------------
# PlayCandidateAnalyzer — state-scoped analysis that pre-computes shared sets
# ---------------------------------------------------------------------------


class PlayCandidateAnalyzer:
    """Pre-computes shared issue/PR sets from a OrchestratorState snapshot, then
    exposes issue-availability checks, freshness predicates, and the full
    candidate-plan builder as methods instead of loose functions with repeated
    keyword-argument threading.
    """

    def __init__(self, state: OrchestratorState) -> None:
        self._state = state

        self.open_issues = [i for i in state.open_issues if i.state.upper() == "OPEN"]
        self.open_prs = [pr for pr in state.pull_requests if pr.state.upper() == "OPEN"]

        self.open_pr_issue_numbers: set[int] = {
            n for pr in self.open_prs for n in issue_numbers_for_pr(pr)
        }
        self.merged_pr_issue_numbers: set[int] = {
            n
            for pr in state.pull_requests
            if pr.state.upper() == "MERGED"
            for n in issue_numbers_for_pr(pr)
        }
        self.in_flight_issue_numbers: set[int] = set(state.in_flight_issues)

        self.blocked_issue_numbers: set[int] = {
            i.issue_number
            for i in self.open_issues
            if "agentshore/blocked" in i.labels or "blocked" in i.labels
        }
        self.disallowed_issue_numbers: set[int] = {
            i.issue_number for i in self.open_issues if DISALLOWED_LABEL in i.labels
        }
        # Opt-in issue-author gating: when enabled, only issues authored by a
        # trusted login (configured logins ∪ enabled agents' own identities) are
        # workable. The toggle and the resolved trusted set are carried on the
        # state (assembled once per tick from config), so this stays pure and
        # state-only. Off by default → empty set, behavior unchanged.
        self.untrusted_issue_numbers: set[int] = set()
        if state.restrict_issues_to_trusted_authors:
            trusted = state.trusted_issue_authors
            for issue in self.open_issues:
                author = (
                    canonical_identity_name(issue.github_author) if issue.github_author else None
                )
                if author is None or author not in trusted:
                    self.untrusted_issue_numbers.add(issue.issue_number)
                    _logger.info(
                        "github_issue_ignored",
                        reason="untrusted_author",
                        issue_number=issue.issue_number,
                        author=issue.github_author,
                    )

        # Graph-derived state
        graph = state.graph
        self._graph_has_epics = bool(
            graph is not None and getattr(graph, "has_epics", False) is True
        )
        graph_has_ready_tasks = bool(
            graph is not None and getattr(graph, "has_ready_tasks", False) is True
        )
        raw_tasks = getattr(graph, "tasks", ()) if graph is not None else ()
        try:
            self._graph_tasks: list[object] = list(raw_tasks or ())
        except TypeError:
            self._graph_tasks = []
        raw_ready = getattr(graph, "tasks_ready", 0) if graph is not None else 0
        self.ready_task_count: int = raw_ready if isinstance(raw_ready, int) else 0
        self.beads_blocks_issue_pickup: bool = self._graph_has_epics and not graph_has_ready_tasks

        self.bead_in_progress_issue_numbers: set[int] = in_progress_issue_numbers(state)

        self.bead_blocked_issue_numbers: set[int] = {
            getattr(task, "issue_number")  # noqa: B009
            for task in self._graph_tasks
            if getattr(task, "issue_number", None) is not None
            and bool(getattr(task, "blocked_by_ids", None))
        }

        # Backlog sync
        tracked = {
            getattr(task, "issue_number")  # noqa: B009
            for task in self._graph_tasks
            if getattr(task, "issue_number", None) is not None
        }
        sync_candidates = {
            i.issue_number
            for i in self.open_issues
            if i.issue_number not in self.blocked_issue_numbers
            and i.issue_number not in self.disallowed_issue_numbers
            and i.issue_number not in self.in_flight_issue_numbers
        }
        self._untracked_gh_issue_numbers: set[int] = (
            sync_candidates - tracked if self._graph_has_epics else set()
        )
        self._unlinked_ready_task_count: int = (
            sum(
                1
                for t in self._graph_tasks
                if getattr(t, "ready", False) is True and getattr(t, "issue_number", None) is None
            )
            if self._graph_has_epics
            else 0
        )
        self.backlog_sync_work_count: int = (
            len(self._untracked_gh_issue_numbers) + self._unlinked_ready_task_count
        )

    # -- issue availability --------------------------------------------------

    def issue_available_for_plan(self, issue: IssueSnapshot) -> bool:
        labels = set(issue.labels)
        return (
            issue.state.upper() == "OPEN"
            and self._base_issue_available(issue)
            and not (PLANNED_LABELS & labels)
            and "agentshore/needs-refinement" not in labels
            and issue.issue_number not in self._state.planned_issues
        )

    def issue_available_for_pickup(self, issue: IssueSnapshot) -> bool:
        labels = set(issue.labels)
        return (
            issue.state.upper() == "OPEN"
            and issue.issue_number not in self.open_pr_issue_numbers
            and issue.issue_number not in self.merged_pr_issue_numbers
            and issue.issue_number not in self.in_flight_issue_numbers
            and issue.issue_number not in self.bead_in_progress_issue_numbers
            and issue.issue_number not in self.bead_blocked_issue_numbers
            and not self.beads_blocks_issue_pickup
            and not (ISSUE_PICKUP_SKIP_LABELS & labels)
            and issue.issue_number not in self.untrusted_issue_numbers
        )

    def issue_available_for_refine(self, issue: IssueSnapshot) -> bool:
        return (
            issue.state.upper() == "OPEN"
            and self._base_issue_available(issue)
            and "agentshore/needs-refinement" in issue.labels
            # An issue refine has already processed carries agentshore/refined.
            # Without this, refine is re-selected on already-refined issues and
            # an agent is dispatched only to no-op ("all issues already
            # refined"). Re-armed by groom/design-audit removing the label.
            and "agentshore/refined" not in issue.labels
        )

    def issue_available_for_debug(self, issue: IssueSnapshot) -> bool:
        labels = set(issue.labels)
        return (
            issue.state.upper() == "OPEN"
            and self._base_issue_available(issue)
            and bool(DEBUG_TRIGGER_LABELS & labels)
            and ROOT_CAUSE_FOUND_LABEL not in labels
        )

    def _base_issue_available(self, issue: IssueSnapshot) -> bool:
        labels = set(issue.labels)
        return (
            issue.issue_number not in self.open_pr_issue_numbers
            and issue.issue_number not in self.merged_pr_issue_numbers
            and issue.issue_number not in self.in_flight_issue_numbers
            # An issue whose beads task is already in_progress is owned by a
            # live PR/agent. ``issue_available_for_pickup`` excludes it inline;
            # plan/refine/debug route through here, so excluding it here keeps
            # them in parity with pickup and with the dispatch-time live-beads
            # gate. Without this, write_implementation_plan re-selects the same
            # in_progress issue every tick (deterministic priority sort) and is
            # bounced at dispatch, starving other workable issues.
            and issue.issue_number not in self.bead_in_progress_issue_numbers
            and issue.issue_number not in self.untrusted_issue_numbers
            and "agentshore/blocked" not in labels
            and "blocked" not in labels
            and DISALLOWED_LABEL not in labels
            # An issue the planner gave up on (un-plannable, #458) is parked for
            # a human; exclude it from plan/pickup/refine/debug so it stops being
            # re-selected every tick. Cleared when the label is removed.
            and NEEDS_HUMAN_LABEL not in labels
        )

    # -- freshness / terminal predicates -------------------------------------

    def beads_groom_needed(self) -> bool:
        if not self._graph_has_epics:
            return False
        return self.backlog_sync_work_count > 0 or (
            self.beads_blocks_issue_pickup
            and any(
                issue.state.upper() == "OPEN"
                and issue.issue_number not in self._state.in_flight_issues
                and "agentshore/blocked" not in issue.labels
                and "blocked" not in issue.labels
                and DISALLOWED_LABEL not in issue.labels
                for issue in self._state.open_issues
            )
        )

    def seed_audit_is_fresh(self) -> bool:
        return seed_audit_is_fresh(self._state)

    def design_audit_is_fresh(self, *, window: int = DESIGN_AUDIT_FRESHNESS_WINDOW_PLAYS) -> bool:
        return design_audit_is_fresh(self._state, window=window)

    def terminal_audits_are_fresh(self) -> bool:
        return terminal_audits_are_fresh(self._state)

    def qa_ran_within_terminal_window(
        self, *, window: int = TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS
    ) -> bool:
        return qa_ran_within_terminal_window(self._state, window=window)

    # -- candidate plan builder ----------------------------------------------

    def build(self) -> PlayCandidatePlan:
        """Build a pure, state-only candidate plan for PPO-safe consumers."""

        state = self._state
        candidates: dict[PlayType, list[PlayCandidate]] = {}
        blocked: dict[PlayType, list[str]] = {}
        active_keys = active_resource_keys(state)
        parked_keys = state.parked_resource_keys

        def add(candidate: PlayCandidate) -> None:
            # Piece A: a resource parked after repeated worktree-allocation
            # failures is excluded from every play that touches it, so a
            # structurally-unallocatable PR can't be re-selected each tick.
            parked_hit = sorted(set(candidate.resource_keys) & parked_keys)
            if parked_hit:
                reasons = blocked.setdefault(candidate.play_type, [])
                msg = f"resource parked (worktree allocation failed): {', '.join(parked_hit)}"
                if msg not in reasons:
                    reasons.append(msg)
                return
            conflict = resource_conflict_reason(candidate.resource_keys, active_keys)
            if conflict is not None:
                reasons = blocked.setdefault(candidate.play_type, [])
                if conflict not in reasons:
                    reasons.append(conflict)
                return
            candidates.setdefault(candidate.play_type, []).append(candidate)

        covered_by_open_pr_numbers = {
            i.issue_number for i in self.open_issues if i.issue_number in self.open_pr_issue_numbers
        }
        resolved_by_merged_pr_numbers = {
            i.issue_number
            for i in self.open_issues
            if i.issue_number in self.merged_pr_issue_numbers
        }
        in_flight_numbers = {
            i.issue_number
            for i in self.open_issues
            if i.issue_number in self.in_flight_issue_numbers
        }

        for issue in self.open_issues:
            if self.issue_available_for_plan(issue):
                add(_issue_candidate(PlayType.WRITE_IMPLEMENTATION_PLAN, issue, source="state"))
            if self.issue_available_for_pickup(issue):
                add(_issue_candidate(PlayType.ISSUE_PICKUP, issue, source="state"))
            if self.issue_available_for_refine(issue):
                add(_issue_candidate(PlayType.REFINE_TASK_BREAKDOWN, issue, source="state"))
            if self.issue_available_for_debug(issue):
                add(_issue_candidate(PlayType.SYSTEMATIC_DEBUGGING, issue, source="state"))

        in_flight_review_prs = _in_flight_prs(state, PlayType.CODE_REVIEW)
        pr_by_number = {pr.pr_number: pr for pr in state.pull_requests}

        def _pr_manual_required(pr_number: int) -> bool:
            # A manual-required PR is parked for human intervention — never an
            # actionable review target (mirrors pr_reviewable). _labels(None)
            # returns [] so a queue row without a PR record is treated as not
            # manual-required.
            return MANUAL_REQUIRED_LABEL in _labels(pr_by_number.get(pr_number))

        pending_pr_numbers = {
            row.pr_number
            for row in state.pending_review_queue
            if not _pr_manual_required(row.pr_number)
            and resource_conflict_reason(
                pr_resource_keys_for_pr(pr_by_number[row.pr_number])
                if row.pr_number in pr_by_number
                else pr_resource_keys(row.pr_number),
                active_keys,
            )
            is None
        }
        for index, row in enumerate(state.pending_review_queue):
            if row.pr_number in in_flight_review_prs or _pr_manual_required(row.pr_number):
                continue
            pr = pr_by_number.get(row.pr_number)
            resource_keys = (
                pr_resource_keys_for_pr(pr) if pr is not None else pr_resource_keys(row.pr_number)
            )
            add(
                PlayCandidate(
                    play_type=PlayType.CODE_REVIEW,
                    params=PlayParams(
                        pr_number=row.pr_number,
                        branch=pr.branch if pr is not None else None,
                        extras={"review_queue_id": row.queue_id}
                        if row.queue_id is not None
                        else {},
                    ),
                    resource_keys=resource_keys,
                    source="pending_review_queue",
                    sort_key=(0, index, row.pr_number),
                )
            )
        if not state.pending_review_queue:
            for candidate in _eligible_pr_candidates(
                self.open_prs,
                excluded=in_flight_review_prs,
                predicate=pr_reviewable,
                make_candidate=lambda index, pr, keys: PlayCandidate(
                    play_type=PlayType.CODE_REVIEW,
                    params=PlayParams(pr_number=pr.pr_number, branch=pr.branch),
                    resource_keys=keys,
                    source="state",
                    sort_key=(1, 0 if pr.last_review_status else 1, index, pr.pr_number),
                ),
            ):
                add(candidate)

        for candidate in _eligible_pr_candidates(
            self.open_prs,
            excluded=_in_flight_prs(state, PlayType.MERGE_PR),
            predicate=lambda pr: pr_merge_ready(pr, target_branch=state.target_branch),
            make_candidate=lambda index, pr, keys: PlayCandidate(
                play_type=PlayType.MERGE_PR,
                params=PlayParams(pr_number=pr.pr_number, branch=pr.branch),
                resource_keys=keys,
                source="state",
                sort_key=(index, pr.pr_number),
            ),
            trunk_scoped=True,
        ):
            add(candidate)

        for candidate in _eligible_pr_candidates(
            self.open_prs,
            excluded=_in_flight_prs(state, PlayType.UNBLOCK_PR),
            predicate=pr_unblockable,
            make_candidate=lambda index, pr, keys: PlayCandidate(
                play_type=PlayType.UNBLOCK_PR,
                params=PlayParams(pr_number=pr.pr_number, branch=pr.branch),
                resource_keys=keys,
                source="state",
                sort_key=(index, pr.pr_number),
            ),
        ):
            add(candidate)

        groom_needed = self.beads_groom_needed()
        if groom_needed:
            add(
                PlayCandidate(
                    play_type=PlayType.GROOM_BACKLOG,
                    params=PlayParams(),
                    # groom_backlog only updates beads metadata — it must not
                    # take the trunk writer lock (would starve merge_pr, #17).
                    # Self-serialize on a session key so two grooms don't race.
                    resource_keys=(f"session:{PlayType.GROOM_BACKLOG.value}",),
                    source="state",
                    sort_key=(0,),
                )
            )
        else:
            blocked[PlayType.GROOM_BACKLOG] = ["no beads backlog-sync or groom work detected"]

        sorted_candidates = {
            play_type: tuple(sorted(play_candidates, key=lambda c: c.sort_key))
            for play_type, play_candidates in candidates.items()
        }
        if not sorted_candidates.get(PlayType.ISSUE_PICKUP) and self.bead_in_progress_issue_numbers:
            blocked[PlayType.ISSUE_PICKUP] = [
                f"beads task for gh-{n} is already in_progress"
                for n in sorted(self.bead_in_progress_issue_numbers)
            ]

        planning_count = len(sorted_candidates.get(PlayType.WRITE_IMPLEMENTATION_PLAN, ()))
        implementation_count = len(sorted_candidates.get(PlayType.ISSUE_PICKUP, ()))
        refinement_count = len(sorted_candidates.get(PlayType.REFINE_TASK_BREAKDOWN, ()))
        debugging_count = len(sorted_candidates.get(PlayType.SYSTEMATIC_DEBUGGING, ()))
        workable_issue_numbers = {
            c.params.issue_number
            for pt in (
                PlayType.WRITE_IMPLEMENTATION_PLAN,
                PlayType.ISSUE_PICKUP,
                PlayType.REFINE_TASK_BREAKDOWN,
                PlayType.SYSTEMATIC_DEBUGGING,
            )
            for c in sorted_candidates.get(pt, ())
            if c.params.issue_number is not None
        }
        reviewable_pr_numbers = {
            c.params.pr_number
            for c in sorted_candidates.get(PlayType.CODE_REVIEW, ())
            if c.params.pr_number is not None
        } | pending_pr_numbers
        mergeable_pr_numbers = {
            c.params.pr_number
            for c in sorted_candidates.get(PlayType.MERGE_PR, ())
            if c.params.pr_number is not None
        }
        unblockable_pr_numbers = {
            c.params.pr_number
            for c in sorted_candidates.get(PlayType.UNBLOCK_PR, ())
            if c.params.pr_number is not None
        }
        actionable_pr_numbers = (
            reviewable_pr_numbers | mergeable_pr_numbers | unblockable_pr_numbers
        )
        manual_required_open_pr_count = sum(
            1 for pr in self.open_prs if MANUAL_REQUIRED_LABEL in _labels(pr)
        )
        has_actionable_work = (
            planning_count > 0
            or implementation_count > 0
            or refinement_count > 0
            or debugging_count > 0
            or bool(actionable_pr_numbers)
            or self.backlog_sync_work_count > 0
            or groom_needed
        )
        # The open-PR cap blocks new issue_pickup; when (cap - 1) of those PRs are
        # parked for a human, the queue cannot drain into mergeable work and the
        # session is wedged on human action — surface that so END_SESSION becomes
        # a valid terminal choice even while nominal issue/task work still looks
        # plannable (#166). Also fire when *every* open PR is manual-required AND
        # there is no other actionable work: with no selectable PR work and the
        # remaining graph "ready tasks" all covered by those parked PRs, the
        # session is equally wedged on a human even below the cap. This is the
        # end-session-wedge fix — 4-of-4 manual-required PRs with phantom ready
        # tasks slipped under the cap-only threshold and stranded the loop with
        # END_SESSION masked. The ``not has_actionable_work`` guard keeps the
        # hatch closed while genuine issue/PR work still remains to do.
        open_pr_count = len(self.open_prs)
        all_open_prs_manual_required = (
            open_pr_count > 0 and manual_required_open_pr_count == open_pr_count
        )
        pr_queue_human_blocked = manual_required_open_pr_count >= MAX_OPEN_PRS - 1 or (
            all_open_prs_manual_required and not has_actionable_work
        )
        terminal_no_work = (
            self._graph_has_epics
            and self.terminal_audits_are_fresh()
            and not state.in_flight_plays
            and not has_actionable_work
        )
        has_remaining_work = (
            has_actionable_work
            or (self._graph_has_epics and not self.terminal_audits_are_fresh())
            or (terminal_no_work and not self.qa_ran_within_terminal_window())
        )
        availability = WorkAvailability(
            tracked_issue_count=len(state.open_issues),
            github_open_issue_count=len(self.open_issues),
            workable_issue_count=len(workable_issue_numbers),
            blocked_issue_count=len(self.blocked_issue_numbers),
            disallowed_issue_count=len(self.disallowed_issue_numbers),
            untrusted_issue_count=len(self.untrusted_issue_numbers),
            pull_requests_hidden_count=state.ignored_pr_count,
            covered_by_open_pr_count=len(covered_by_open_pr_numbers),
            resolved_by_merged_pr_count=len(resolved_by_merged_pr_numbers),
            in_flight_issue_count=len(in_flight_numbers),
            bead_in_progress_issue_count=len(self.bead_in_progress_issue_numbers),
            bead_blocked_issue_count=len(self.bead_blocked_issue_numbers),
            ready_task_count=self.ready_task_count,
            beads_blocks_issue_pickup=self.beads_blocks_issue_pickup,
            untracked_gh_issue_count=len(self._untracked_gh_issue_numbers),
            unlinked_ready_task_count=self._unlinked_ready_task_count,
            backlog_sync_work_count=self.backlog_sync_work_count,
            planning_eligible_count=planning_count,
            implementation_eligible_count=implementation_count,
            refinement_eligible_count=refinement_count,
            debugging_eligible_count=debugging_count,
            reviewable_pr_count=len(reviewable_pr_numbers),
            mergeable_pr_count=len(mergeable_pr_numbers),
            unblockable_pr_count=len(unblockable_pr_numbers),
            actionable_pr_work_count=len(actionable_pr_numbers),
            manual_required_open_pr_count=manual_required_open_pr_count,
            pr_queue_human_blocked=pr_queue_human_blocked,
            terminal_no_work=terminal_no_work,
        )
        return PlayCandidatePlan(
            candidates_by_play_type=sorted_candidates,
            blocked_reasons_by_play_type={pt: tuple(reasons) for pt, reasons in blocked.items()},
            work_availability=availability,
            has_remaining_work=has_remaining_work,
        )


# ---------------------------------------------------------------------------
# State-only audit-freshness predicates
#
# These read only ``state`` (cooldown counters), so they are genuine
# module-level functions rather than analyzer methods — callers on the hot
# RL mask path (``rl/mask.py``) must not allocate a full ``PlayCandidateAnalyzer``
# (which eagerly computes issue/PR sets) just to read one counter.
# ---------------------------------------------------------------------------


def seed_audit_is_fresh(state: OrchestratorState) -> bool:
    """Return True when a successful seed audit is still inside cooldown."""
    if state.last_play_success_by_type.get(PlayType.SEED_PROJECT) is not True:
        return False
    plays_since = state.plays_since_last_play_type.get(PlayType.SEED_PROJECT)
    return plays_since is not None and plays_since < SEED_PROJECT_COOLDOWN_PLAYS


def design_audit_is_fresh(
    state: OrchestratorState, *, window: int = DESIGN_AUDIT_FRESHNESS_WINDOW_PLAYS
) -> bool:
    """Return True when a successful design audit is still inside the requested window."""
    if state.last_play_success_by_type.get(PlayType.DESIGN_AUDIT) is not True:
        return False
    plays_since = state.plays_since_last_play_type.get(PlayType.DESIGN_AUDIT)
    return plays_since is not None and plays_since < window


def terminal_audits_are_fresh(state: OrchestratorState) -> bool:
    """Return True when the audit posture justifies a clean shutdown.

    Two paths into True:
      - Seeded sessions: BOTH seed_project AND design_audit recent
        (the original gate — preserves end_session evidence for the
        normal lifecycle where SEED_PROJECT runs at bootstrap).
      - Open-start sessions: SEED_PROJECT was never run in this
        session at all, so only design_audit recency is required.
        Without this fallback the failsafe is structurally unreachable
        in open-start mode (observed 2026-05-28 session 08a948ed:
        150 plays, $43 spent, end_session permanently masked because
        no seed audit ever fired).
    """
    if not design_audit_is_fresh(state, window=TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS):
        return False
    seed_ever_succeeded = state.last_play_success_by_type.get(PlayType.SEED_PROJECT) is True
    if not seed_ever_succeeded:
        return True
    return seed_audit_is_fresh(state)


def qa_ran_within_terminal_window(
    state: OrchestratorState, *, window: int = TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS
) -> bool:
    """Return True when successful RUN_QA is recent enough to end a no-work session."""
    if state.last_play_success_by_type.get(PlayType.RUN_QA) is not True:
        return False
    plays_since = state.plays_since_last_play_type.get(PlayType.RUN_QA)
    return plays_since is not None and plays_since < window


def build_candidate_plan(state: OrchestratorState) -> PlayCandidatePlan:
    """Build a pure, state-only candidate plan for PPO-safe consumers.

    Issue-author trust gating (opt-in
    ``trusted_ids.restrict_issues_to_trusted_authors``) is driven entirely off
    the state: ``state.restrict_issues_to_trusted_authors`` and
    ``state.trusted_issue_authors`` are resolved once per tick at state assembly,
    so every consumer of this plan gates consistently with no config threading.
    """
    return PlayCandidateAnalyzer(state).build()


class PlayCandidateService:
    """Resolver-time candidate discovery, including store and live-GitHub fallbacks."""

    def __init__(
        self,
        *,
        store: DataStore,
        cfg: RuntimeConfig,
        github: GitHubAdapter | None = None,
        project_path: Path | None = None,
        unblock_failures: dict[int, int] | None = None,
        unblock_exhaustion_threshold: int = 3,
    ) -> None:
        self._store = store
        self._cfg = cfg
        self._github = github
        self._project_path = project_path
        self._unblock_failures = unblock_failures if unblock_failures is not None else {}
        self._unblock_exhaustion_threshold = unblock_exhaustion_threshold

    async def candidates_for(
        self,
        play_type: PlayType,
        state: OrchestratorState,
        *,
        idle_reviewers: list[AgentSnapshot] | None = None,
    ) -> list[PlayCandidate]:
        if play_type in {
            PlayType.WRITE_IMPLEMENTATION_PLAN,
            PlayType.SYSTEMATIC_DEBUGGING,
            PlayType.REFINE_TASK_BREAKDOWN,
        }:
            return list(build_candidate_plan(state).candidates_for(play_type))
        if play_type == PlayType.ISSUE_PICKUP:
            return await self._issue_pickup_candidates(state)
        if play_type == PlayType.CODE_REVIEW:
            return await self._code_review_candidates(state, idle_reviewers or [])
        if play_type == PlayType.MERGE_PR:
            return await self._merge_pr_candidates(state)
        if play_type == PlayType.UNBLOCK_PR:
            return await self._unblock_pr_candidates(state)
        return list(build_candidate_plan(state).candidates_for(play_type))

    async def _issue_pickup_candidates(self, state: OrchestratorState) -> list[PlayCandidate]:
        candidates = list(build_candidate_plan(state).candidates_for(PlayType.ISSUE_PICKUP))
        if not candidates:
            return []

        if (
            state.graph is not None
            and state.graph.has_ready_tasks
            and self._project_path is not None
        ):
            beads = await ready_tasks(self._project_path)
            ready_issue_numbers: set[int] = set()
            for bead in beads:
                ref = bead.external_ref
                if ref and ref.startswith("gh-"):
                    with contextlib.suppress(ValueError):
                        ready_issue_numbers.add(int(ref[3:]))
            bead_candidates = [
                candidate
                for candidate in candidates
                if candidate.params.issue_number in ready_issue_numbers
            ]
            if bead_candidates:
                return bead_candidates

        return candidates

    async def _code_review_candidates(
        self,
        state: OrchestratorState,
        idle_reviewers: list[AgentSnapshot],
    ) -> list[PlayCandidate]:
        pending = await self._store.list_pending_reviews(state.session_id)
        in_flight_review_prs = _in_flight_prs(state, PlayType.CODE_REVIEW)
        active_keys = active_resource_keys(state)
        pr_by_number = {pr.pr_number: pr for pr in state.pull_requests}
        candidates: list[PlayCandidate] = []

        if pending:
            for index, row in enumerate(pending):
                if row.pr_number in in_flight_review_prs:
                    continue
                pr = pr_by_number.get(row.pr_number)
                if pr is None and row.queue_id is not None:
                    await self._store.complete_review(row.queue_id)
                    continue
                # A manual-required PR is parked for a human — never dispatch a
                # reviewer at it (mirrors pr_reviewable / the build_candidate_plan
                # filter), so it can't churn the review queue (#167).
                if pr is not None and MANUAL_REQUIRED_LABEL in _labels(pr):
                    continue
                reviewer = pick_reviewer_for_pr(
                    pr.github_author if pr is not None else None,
                    idle_reviewers,
                )
                if reviewer is None:
                    continue
                resource_keys = (
                    pr_resource_keys_for_pr(pr)
                    if pr is not None
                    else pr_resource_keys(row.pr_number)
                )
                if resource_conflict_reason(resource_keys, active_keys) is not None:
                    continue
                candidates.append(
                    PlayCandidate(
                        play_type=PlayType.CODE_REVIEW,
                        params=PlayParams(
                            pr_number=row.pr_number,
                            branch=pr.branch if pr is not None else None,
                            target_agent_id=reviewer.agent_id,
                            extras={"review_queue_id": row.queue_id}
                            if row.queue_id is not None
                            else {},
                        ),
                        resource_keys=resource_keys,
                        source="pending_review_queue",
                        sort_key=(0, index, row.pr_number),
                    )
                )
            if candidates:
                return candidates

            queued_pr_numbers = {row.pr_number for row in pending}
            excluded = queued_pr_numbers | in_flight_review_prs | _already_reviewed_prs(state)
            return await self._github_code_review_candidates(
                state,
                idle_reviewers,
                excluded=excluded,
                source="github_pending_fallback",
            )

        plan_candidates = build_candidate_plan(state).candidates_for(PlayType.CODE_REVIEW)
        for candidate in plan_candidates:
            pr = pr_by_number.get(candidate.params.pr_number or -1)
            reviewer = pick_reviewer_for_pr(
                pr.github_author if pr is not None else None,
                idle_reviewers,
            )
            if reviewer is None:
                continue
            candidates.append(
                replace(
                    candidate,
                    params=replace(candidate.params, target_agent_id=reviewer.agent_id),
                )
            )
        if candidates:
            return candidates

        excluded = _already_reviewed_prs(state) | in_flight_review_prs
        return await self._github_code_review_candidates(
            state,
            idle_reviewers,
            excluded=excluded,
            source="github_fallback",
        )

    async def _merge_pr_candidates(self, state: OrchestratorState) -> list[PlayCandidate]:
        in_flight_merge_prs = _in_flight_prs(state, PlayType.MERGE_PR)
        target_branch = self._cfg.project.target_branch

        def is_mergeable(pr: object) -> bool:
            return pr_merge_ready(pr, target_branch=target_branch)

        # Store-backed pass: same eligibility/conflict pipeline as build(), only
        # the PR source (approved PRs in the store) is resolver-specific.
        candidates = _pr_play_candidates(
            await self._store.list_approved_pull_requests(state.session_id),
            excluded=in_flight_merge_prs,
            predicate=is_mergeable,
            make_candidate=lambda index, pr, keys: PlayCandidate(
                play_type=PlayType.MERGE_PR,
                params=PlayParams(pr_number=pr.pr_number, branch=pr.branch),
                resource_keys=keys,
                source="store",
                sort_key=(index, pr.pr_number),
            ),
            active_keys=active_resource_keys(state),
            trunk_scoped=True,
        )
        if candidates:
            return candidates

        return await self._github_pr_candidates(
            state,
            PlayType.MERGE_PR,
            lambda pr: pr.pr_number not in in_flight_merge_prs and is_mergeable(pr),
            limit=5,
            log_key="github_pr_resolve_failed",
        )

    async def _unblock_pr_candidates(self, state: OrchestratorState) -> list[PlayCandidate]:
        in_flight_unblock_prs = _in_flight_prs(state, PlayType.UNBLOCK_PR)
        exhausted = {
            pr_num
            for pr_num, count in self._unblock_failures.items()
            if count >= self._unblock_exhaustion_threshold
        }
        excluded = in_flight_unblock_prs | exhausted
        prs = list(await self._store.list_open_pull_requests(state.session_id))
        random.shuffle(prs)
        # Store-backed pass: same eligibility/conflict pipeline as build(), only
        # the PR source (open PRs in the store, shuffled) is resolver-specific.
        candidates = _pr_play_candidates(
            prs,
            excluded=excluded,
            predicate=pr_unblockable,
            make_candidate=lambda index, pr, keys: PlayCandidate(
                play_type=PlayType.UNBLOCK_PR,
                params=PlayParams(pr_number=pr.pr_number, branch=pr.branch),
                resource_keys=keys,
                source="store",
                sort_key=(index, pr.pr_number),
            ),
            active_keys=active_resource_keys(state),
        )
        if candidates:
            return candidates

        return await self._github_pr_candidates(
            state,
            PlayType.UNBLOCK_PR,
            lambda pr: pr.pr_number not in excluded and pr_unblockable(pr),
            limit=20,
            log_key="github_blocked_pr_resolve_failed",
        )

    async def _github_code_review_candidates(
        self,
        state: OrchestratorState,
        idle_reviewers: list[AgentSnapshot],
        *,
        excluded: set[int],
        source: str,
        limit: int = 5,
    ) -> list[PlayCandidate]:
        if self._github is None:
            return []
        try:
            prs = await self._github.list_pull_requests(state="open", limit=limit)
            prs = filter_trusted_pull_requests(
                prs,
                self._cfg,
                context="resolver_code_review_fallback",
            )
            candidates: list[PlayCandidate] = []
            active_keys = active_resource_keys(state)
            for index, pr in enumerate(prs):
                if pr.pr_number in excluded or MANUAL_REQUIRED_LABEL in _labels(pr):
                    continue
                reviewer = pick_reviewer_for_pr(pr.github_author, idle_reviewers)
                if reviewer is None:
                    continue
                resource_keys = pr_resource_keys_for_pr(pr)
                if resource_conflict_reason(resource_keys, active_keys) is not None:
                    continue
                candidates.append(
                    PlayCandidate(
                        play_type=PlayType.CODE_REVIEW,
                        params=PlayParams(
                            pr_number=pr.pr_number,
                            branch=pr.branch,
                            target_agent_id=reviewer.agent_id,
                        ),
                        resource_keys=resource_keys,
                        source=source,
                        sort_key=(index, pr.pr_number),
                    )
                )
            return candidates
        except (OSError, TimeoutError, json.JSONDecodeError, KeyError, ValueError) as exc:
            _logger.warning("github_code_review_fallback_failed", error=str(exc))
            return []

    async def _github_pr_candidates(
        self,
        state: OrchestratorState,
        play_type: PlayType,
        predicate: Callable[[PullRequestRecord], bool],
        *,
        limit: int,
        log_key: str,
    ) -> list[PlayCandidate]:
        if self._github is None:
            return []
        try:
            prs = await self._github.list_pull_requests(state="open", limit=limit)
            prs = filter_trusted_pull_requests(prs, self._cfg, context=log_key)
            return _pr_play_candidates(
                prs,
                excluded=set(),
                predicate=predicate,
                make_candidate=lambda index, pr, keys: PlayCandidate(
                    play_type=play_type,
                    params=PlayParams(pr_number=pr.pr_number, branch=pr.branch),
                    resource_keys=keys,
                    source="github_fallback",
                    sort_key=(index, pr.pr_number),
                ),
                active_keys=active_resource_keys(state),
            )
        except (OSError, TimeoutError, json.JSONDecodeError, KeyError, ValueError) as exc:
            _logger.warning(log_key, error=str(exc))
            return []


def idle_can_review_agents(state: OrchestratorState) -> list[AgentSnapshot]:
    """Idle, can_review, tier-eligible agents sorted for deterministic pinning."""

    allowed = allowed_tiers_for(PlayType.CODE_REVIEW) or frozenset()
    eligible = [
        agent
        for agent in state.agents
        if agent.status == AgentStatus.IDLE
        and bool(AGENT_CAPABILITIES.get(agent.agent_type, {}).get("can_review", False))
        and (agent.model_tier or DEFAULT_MODEL_TIER) in allowed
        # Circuit breaker (#22): don't pin a review to a known-dead reviewer
        # (the gemini-ETIMEDOUT case — 0 successes, repeated timeouts).
        and not is_agent_circuit_broken(
            tasks_completed=agent.tasks_completed,
            tasks_failed=agent.tasks_failed,
            timeout_count=agent.timeout_count,
            consecutive_timeouts=agent.consecutive_timeouts,
        )
    ]
    eligible.sort(key=lambda agent: (agent.agent_type.value, agent.agent_id))
    return eligible


def pick_reviewer_for_pr(
    pr_github_author: str | None,
    candidates: list[AgentSnapshot],
) -> AgentSnapshot | None:
    """Return an idle reviewer whose GitHub identity differs from the PR author."""

    if not candidates:
        return None
    if pr_github_author is None:
        return candidates[0]
    for candidate in candidates:
        if not same_identity(candidate.github_identity, pr_github_author):
            return candidate
    return None


def _issue_candidate(play_type: PlayType, issue: IssueSnapshot, *, source: str) -> PlayCandidate:
    return PlayCandidate(
        play_type=play_type,
        params=PlayParams(issue_number=issue.issue_number),
        resource_keys=issue_resource_keys(issue.issue_number),
        source=source,
        sort_key=issue_pickup_sort_key(issue),
    )


def _labels(pr: object) -> list[str]:
    labels = getattr(pr, "labels", [])
    return labels if isinstance(labels, list) else []


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _bool_or_none(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _task_status_is(task: object, status: BeadStatus) -> bool:
    value = getattr(task, "status", None)
    return value == status or str(value) == status.value


def in_progress_issue_numbers(state: OrchestratorState) -> set[int]:
    """Issue numbers whose beads task is currently ``in_progress``.

    Single source of truth for the in_progress exclusion shared by the
    candidate-filter layer (``_base_issue_available`` / pickup) and the
    resolver's per-issue eligibility checks, so the two never drift.
    """
    graph = getattr(state, "graph", None)
    raw_tasks = getattr(graph, "tasks", ()) if graph is not None else ()
    try:
        tasks = list(raw_tasks or ())
    except TypeError:
        tasks = []
    return {
        getattr(task, "issue_number")  # noqa: B009
        for task in tasks
        if getattr(task, "issue_number", None) is not None
        and _task_status_is(task, BeadStatus.IN_PROGRESS)
    }


def _in_flight_prs(state: OrchestratorState, play_type: PlayType) -> set[int]:
    return {
        agent.current_play_pr_number
        for agent in state.agents
        if agent.current_play_type == play_type and agent.current_play_pr_number is not None
    }


def _eligible_pr_candidates[PRT](
    prs: Iterable[PRT],
    *,
    excluded: set[int],
    predicate: Callable[[PRT], bool],
    make_candidate: Callable[[int, PRT, tuple[str, ...]], PlayCandidate],
    trunk_scoped: bool = False,
) -> list[PlayCandidate]:
    """Single PR-candidate loop shared by ``build()`` and the resolver service.

    Applies the same exclusion → eligibility-predicate → resource-key pipeline
    regardless of whether ``prs`` comes from live state, the store, or a GitHub
    fallback, so the eligibility rules (``pr_merge_ready`` / ``pr_reviewable`` /
    ``pr_unblockable``) are evaluated in exactly one place. ``make_candidate``
    builds the play-specific ``PlayCandidate`` from ``(index, pr, resource_keys)``.

    In-flight/parked conflict handling is left to the caller: ``build()`` routes
    each result through its ``add`` sink (which records blocked reasons), while
    the resolver service skips conflicts silently via ``_pr_play_candidates``.
    """
    candidates: list[PlayCandidate] = []
    for index, pr in enumerate(prs):
        pr_number = getattr(pr, "pr_number", None)
        if pr_number in excluded or not predicate(pr):
            continue
        resource_keys = pr_resource_keys_for_pr(pr)
        if trunk_scoped:
            resource_keys = (*resource_keys, _TRUNK_RESOURCE_KEY)
        candidates.append(make_candidate(index, pr, resource_keys))
    return candidates


def _pr_play_candidates[PRT](
    prs: Iterable[PRT],
    *,
    excluded: set[int],
    predicate: Callable[[PRT], bool],
    make_candidate: Callable[[int, PRT, tuple[str, ...]], PlayCandidate],
    active_keys: frozenset[str],
    trunk_scoped: bool = False,
) -> list[PlayCandidate]:
    """Resolver-side wrapper: eligible candidates minus in-flight conflicts.

    Adds the silent in-flight-conflict skip the resolver wants on top of the
    shared eligibility loop, so the store/GitHub passes never re-implement the
    predicate iteration that ``build()`` already owns.
    """
    return [
        candidate
        for candidate in _eligible_pr_candidates(
            prs,
            excluded=excluded,
            predicate=predicate,
            make_candidate=make_candidate,
            trunk_scoped=trunk_scoped,
        )
        if resource_conflict_reason(candidate.resource_keys, active_keys) is None
    ]


def _already_reviewed_prs(state: OrchestratorState) -> set[int]:
    return {pr.pr_number for pr in state.pull_requests if not needs_review(pr)}
