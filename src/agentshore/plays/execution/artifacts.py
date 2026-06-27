"""PR and commit artifact recording helpers extracted from plays/executor.py."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.models import ReviewQueueRecord
from agentshore.errors import PreconditionFailed
from agentshore.logging import get_logger
from agentshore.plays._publish_reconciler import _pr_number_from_payload

if TYPE_CHECKING:
    from agentshore.agents.manager import AgentManager
    from agentshore.config import RuntimeConfig
    from agentshore.data.store import DataStore, PullRequestRecord
    from agentshore.github.adapter import GitHubAdapter
    from agentshore.plays.base import PlayParams
    from agentshore.state import OrchestratorState, PlayOutcome, PlayType

_logger = get_logger(__name__)


def _resolve_pr_author(
    manager: AgentManager,
    agent_id: str,
) -> tuple[str | None, str | None]:
    """Return (agent_type, github_login) for the PR author, or (None, None).

    Falls back to (None, None) when the agent has already terminated: the
    executor's identity check treats author=None as "any reviewer eligible"
    and the next GitHub state refresh will populate github_author.
    """
    try:
        handle = manager.get_handle(agent_id)
    except (PreconditionFailed, KeyError):
        return None, None
    return handle.agent_type.value, handle.github_identity


async def _retarget_pr_to_target(
    github: GitHubAdapter | None,
    cfg: RuntimeConfig,
    session_id: str,
    pr_number: int,
    current_base: str | None,
    *,
    idempotency_prefix: str,
    success_event: str,
    failure_event: str,
    log_fields: dict[str, str] | None = None,
) -> bool:
    """Retarget *pr_number* from *current_base* to the configured target.

    Single source for both the pre-dispatch self-heal
    (:func:`_maybe_retarget_pr_base`) and the create-time correction in
    :func:`_record_pr_artifact`. Returns True only when a retarget was issued
    and GitHub reported success. No-op (returns False) when GitHub is
    unavailable, no ``project.target_branch`` is configured, the base is
    unknown, or it already matches the target. Idempotent via the mutation
    ledger keyed on ``<idempotency_prefix>:<pr>:<from>-><to>``.
    """
    if github is None:
        return False
    target = cfg.project.target_branch
    if not target or not current_base or current_base == target:
        return False
    retargeted = await github.retarget_pr_base(
        pr_number,
        target,
        idempotency_key=f"{idempotency_prefix}:{pr_number}:{current_base}->{target}",
    )
    _logger.info(
        success_event if retargeted else failure_event,
        pr_number=pr_number,
        from_base=current_base,
        to_base=target,
        session_id=session_id,
        **(log_fields or {}),
    )
    return retargeted


async def _confirm_and_record_pr(
    github: GitHubAdapter | None,
    cfg: RuntimeConfig,
    store: DataStore,
    session_id: str,
    pr_number: int,
    issue_number: int | None,
    author_agent_id: str,
    author_agent_type: str | None,
    author_github_login: str | None,
) -> PullRequestRecord | None:
    """Confirm the PR exists on GitHub, then record the enriched row.

    The single confirmation gate for the PR mirror: a PR row is written only
    when GitHub returns a real object for ``pr_number``. Returns the recorded
    record on confirmation, or ``None`` when GitHub cannot confirm it — adapter
    unavailable, a transient fetch failure, or a 404 for a number the agent
    reported but never actually opened (an issue number, or a hallucinated PR
    number). The caller treats ``None`` as "do not enqueue, do not label", so
    invented PR state can never enter the mirror (#279) and be re-offered to
    code_review forever (the phantom-PR class behind #278).

    On confirmation it also enriches review_decision/mergeable/head_sha/is_draft
    from GitHub (so the next code_review/merge_pr eligibility check sees real
    data, not NULL defaults) and deterministically retargets the PR base to the
    configured target branch.
    """
    if github is None:
        return None
    try:
        enriched = await github.fetch_pull_request_by_number(pr_number)
    except (OSError, RuntimeError, ValueError) as exc:  # pragma: no cover
        _logger.warning("pr_enrichment_failed", pr_number=pr_number, error=str(exc))
        enriched = None
    if enriched is None:
        return None
    # GitHub is source of truth; stamp session, carry issue when closing-refs
    # missed it, overlay locally-resolved authorship for code_review
    # anti-confirmation until the next refresh fills github_author.
    enriched.session_id = session_id
    if enriched.issue_number is None:
        enriched.issue_number = issue_number
    enriched.author_agent_id = author_agent_id
    enriched.author_agent_type = author_agent_type
    if author_github_login is not None:
        enriched.github_author = author_github_login
    # Base correction at creation: agents skip the skill's base step ~1-in-6
    # times, opening against the wrong base. Retarget using the fresh enriched
    # base_ref (not a stale snapshot — the gap in _maybe_retarget_pr_base).
    # Idempotent; pairs with the merge-side gate so a wrong-base PR never lands.
    retargeted = await _retarget_pr_to_target(
        github,
        cfg,
        session_id,
        pr_number,
        enriched.base_ref,
        idempotency_prefix="create_retarget_base",
        success_event="pr_base_auto_corrected",
        failure_event="pr_base_auto_correct_failed",
    )
    if retargeted:
        enriched.base_ref = cfg.project.target_branch
    await store.record_pull_request(enriched)
    return enriched


async def _record_pr_artifact(
    manager: AgentManager,
    github: GitHubAdapter | None,
    cfg: RuntimeConfig,
    store: DataStore,
    session_id: str,
    play_type: PlayType,
    params: PlayParams,
    outcome: PlayOutcome,
    artifact: dict[str, object],
    now: str,
) -> None:
    """Confirm a created PR exists on GitHub, then record, enqueue, and label it.

    Confirm-then-write (#279): the PR is recorded and enqueued for review only
    when GitHub returns a real object for it. An agent-reported PR number GitHub
    can't confirm (never opened, an issue number, a hallucination) is dropped
    with a ``pr_publish_unconfirmed`` warning — invented PR state never enters
    the mirror, so it can't be re-offered to code_review forever (the phantom-PR
    class behind #278).
    """
    pr_number = _pr_number_from_payload(artifact)
    branch = str(artifact.get("branch") or params.branch or "")
    if pr_number is None or not outcome.agent_id:
        return

    if not branch:
        # PR artifact lacked a branch and params had no fallback. GitHub's
        # headRefName is authoritative, but the miss is worth logging (#567).
        _logger.warning(
            "pr_record_missing_branch",
            pr_number=pr_number,
            play_type=play_type.value,
            agent_id=outcome.agent_id,
            artifact_keys=sorted(artifact.keys()),
            params_branch=params.branch,
        )
    manager.record_branch_exposure(branch, outcome.agent_id)

    author_agent_type, author_github_login = _resolve_pr_author(manager, outcome.agent_id)
    confirmed = await _confirm_and_record_pr(
        github,
        cfg,
        store,
        session_id,
        pr_number,
        params.issue_number,
        outcome.agent_id,
        author_agent_type,
        author_github_login,
    )
    if confirmed is None:
        # Unconfirmed: record/enqueue/label nothing. Branch still surfaces via
        # the publish reconciler; next refresh adopts the PR if it lands later.
        _logger.warning(
            "pr_publish_unconfirmed",
            pr_number=pr_number,
            branch=branch or None,
            issue_number=params.issue_number,
            play_type=play_type.value,
            agent_id=outcome.agent_id,
        )
        return
    await store.enqueue_review(
        ReviewQueueRecord(
            pr_number=pr_number,
            session_id=session_id,
            author_label=author_agent_type,
            enqueued_at=now,
        )
    )
    if author_agent_type is not None and github is not None:
        label_name = f"{cfg.intake.label_prefix}author:{author_agent_type}"
        idem_key = f"author_label:pr{pr_number}:{author_agent_type}"
        await github.label_issue(pr_number, [label_name], idem_key)


async def _record_commit_artifact(
    manager: AgentManager,
    store: DataStore,
    session_id: str,
    params: PlayParams,
    outcome: PlayOutcome,
    artifact: dict[str, object],
) -> None:
    """Record branch-commit activity from a ``commit`` artifact."""
    branch = str(artifact.get("branch") or params.branch or "")
    sha = str(artifact.get("sha") or "")
    if branch and outcome.agent_id:
        manager.record_branch_commit(branch, outcome.agent_id, sha)
        await store.update_branch_activity(branch, session_id, outcome.agent_id, sha or None)


async def _maybe_retarget_pr_base(
    github: GitHubAdapter | None,
    cfg: RuntimeConfig,
    session_id: str,
    play_type: PlayType,
    params: PlayParams,
    state: OrchestratorState,
) -> None:
    """Retarget a PR opened against the wrong base to the configured target.

    No-op when GitHub is unavailable, no ``project.target_branch`` is
    configured, the PR's base is unknown, or the base already matches.
    Idempotent via the mutation ledger (see
    :meth:`GitHubAdapter.retarget_pr_base`). Self-heals #8 regardless of
    whether the authoring agent honored the skill's base instruction.
    """
    pr_number = params.pr_number
    if pr_number is None:
        return
    snapshot = next((pr for pr in state.pull_requests if pr.pr_number == pr_number), None)
    base_ref = snapshot.base_ref if snapshot is not None else None
    await _retarget_pr_to_target(
        github,
        cfg,
        session_id,
        pr_number,
        base_ref,
        idempotency_prefix="retarget_base",
        success_event="pr_base_retargeted",
        failure_event="pr_base_retarget_failed",
        log_fields={"play_type": play_type.value},
    )
