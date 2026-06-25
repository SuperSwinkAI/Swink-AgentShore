"""CodeReviewPlay — run agentshore-code-review on a pull request."""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agentshore.data.models import ReviewFeedbackPatternRecord
from agentshore.logging import get_logger
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import CapabilityGate
from agentshore.state import PlayType

_logger = get_logger(__name__)

# Match the SHA inside a skill error like "already reviewed at <sha>".
# Hex range is intentionally permissive (4–40) — the message format already
# constrains us, and very short test SHAs should still match.
_ALREADY_REVIEWED_SHA_RE = re.compile(r"already reviewed at ([0-9a-f]{4,40})")

# A code_review that fast-failed because the target doesn't resolve to a real PR
# on GitHub (phantom — an agent-reported number that was never opened, or an
# issue number). Deliberately narrow: transient failures (timeout, network,
# rate-limit) must still retry, so only definitive "the PR isn't there"
# signatures match. Substring-matched against the lowercased outcome error.
_PHANTOM_PR_ERROR_MARKERS: tuple[str, ...] = (
    "does not exist",
    "could not resolve to a pullrequest",
    "no pull request to review",
    "no pr to review",
    "no open pr exists",
    "no pull request exists",
)


def _is_phantom_pr_error(error: str | None) -> bool:
    """True when *error* says the review target isn't a real PR on GitHub."""
    if not error:
        return False
    haystack = error.lower()
    return any(marker in haystack for marker in _PHANTOM_PR_ERROR_MARKERS)


if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import JsonObject, OrchestratorState, PlayOutcome, SkillResult


def _verdict(result: SkillResult | None) -> str | None:
    """Map a parsed code_review skill result to an AgentShore-side verdict.

    Returns "PASS" when the skill explicitly reports spec_compliance=PASS
    with zero blocking findings. Returns "BLOCK" when the skill ran a real
    review and reported BLOCK or any blocking findings. Returns None when
    the skill skipped, deduped, or failed at runtime — leaves any prior
    verdict unchanged.
    """
    if result is None:
        return None
    sc = result.spec_compliance
    if sc is None or sc.upper() == "SKIP" or result.error:
        return None
    if sc.upper() == "PASS" and (result.blocking_findings or 0) == 0:
        return "PASS"
    return "BLOCK"


def _verdict_from_prior(result: SkillResult | None) -> str | None:
    """Map the skill's surfaced prior-comment verdict to an AgentShore verdict.

    Used on the dedup short-circuit so the play can persist a fresh
    last_review_status from an existing AGENTSHORE_CODE_REVIEW comment instead
    of leaving the column NULL. Returns None when the skill did not surface
    a parseable prior verdict, so callers fall through to the existing
    preserve-existing-status behavior.
    """
    if result is None or result.prior_verdict is None:
        return None
    pv = result.prior_verdict.upper()
    if pv == "PASS" and (result.prior_blocking_findings or 0) == 0:
        return "PASS"
    if pv in ("PASS", "BLOCK"):
        return "BLOCK"
    return None


class CodeReviewPlay(SkillBackedPlay):
    """Review an open pull request.

    Anti-confirmation invariant (first check): the reviewing agent must not be
    the same agent that authored the PR.  A second check is enforced at the
    executor level (defense in depth).

    Candidate validity ("is there a pending review or an unreviewed/stale-review
    open PR?") lives in ``EligibilityAuthority._VALIDITY_FNS`` for
    ``CODE_REVIEW`` and is appended by the base ``preconditions`` adapter. This
    play only declares the capability gate.
    """

    gates = (CapabilityGate("can_review"),)

    # PR-scoped: self-heal the PR base before review so the diff bases correctly.
    retarget_pr_base = True
    # Anti-confirmation violations are a transient timing race (reviewer reassigned
    # between resolve and dispatch); requeue to a later tick instead of penalizing.
    requeue_on_anti_confirmation = True

    @property
    def play_type(self) -> PlayType:
        return PlayType.CODE_REVIEW

    @property
    def skill_name(self) -> str:
        return "agentshore-code-review"

    @property
    def capability(self) -> str | None:
        return "can_review"

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        outcome = await super().execute(state, params, ctx=ctx)
        verdict = _verdict(self._last_skill_result)
        # The skill reports "already reviewed at <sha>" when a prior AgentShore
        # review comment exists for the current HEAD. Persist the SHA + prior
        # verdict so the candidate pipeline routes the PR forward: PASS →
        # merge_pr eligible, BLOCK → unblock_pr eligible.
        if outcome.success and outcome.error and "already reviewed" in outcome.error:
            prior_status = _verdict_from_prior(self._last_skill_result)
            if params.pr_number is not None:
                m = _ALREADY_REVIEWED_SHA_RE.search(outcome.error)
                if m:
                    await ctx.store.update_pr_last_reviewed_sha(
                        params.pr_number,
                        ctx.session_id,
                        m.group(1),
                        status=prior_status,
                    )
            next_step = "merge" if prior_status == "PASS" else "unblock"
            return dataclasses.replace(
                outcome,
                partial=True,
                error=f"already reviewed — PR routes to {next_step}",
            )
        # Persist reviewed SHA + verdict so the precondition masks this PR and
        # merge_pr can gate on internal approval when GitHub reviewDecision is unavailable.
        if outcome.success:
            persisted = False
            for artifact in outcome.artifacts:
                if isinstance(artifact, dict) and artifact.get("type") == "pr":
                    pr_num = artifact.get("number")
                    sha = artifact.get("head_sha")
                    if isinstance(pr_num, int) and isinstance(sha, str):
                        await ctx.store.update_pr_last_reviewed_sha(
                            pr_num, ctx.session_id, sha, status=verdict
                        )
                        persisted = True
                        break
            # Fallback: zero-diff SKIPs omit the type=pr artifact; look up SHA
            # from state to prevent indefinite re-picks on the same PR.
            if not persisted and params.pr_number is not None:
                pr_num = params.pr_number
                for pr in state.pull_requests:
                    if pr.pr_number == pr_num and pr.head_sha:
                        await ctx.store.update_pr_last_reviewed_sha(
                            pr_num, ctx.session_id, pr.head_sha, status=verdict
                        )
                        break
        # Mark the review queue row as done so the resolver stops picking this PR.
        if outcome.success and params.pr_number is not None:
            queue_id = params.extras.get("review_queue_id")
            if isinstance(queue_id, int):
                await ctx.store.complete_review(queue_id)
            else:
                pending = await ctx.store.list_pending_reviews(ctx.session_id)
                for row in pending:
                    if row.pr_number == params.pr_number and row.queue_id is not None:
                        await ctx.store.complete_review(row.queue_id)
                        break
        if outcome.success and self._last_skill_result is not None:
            records = list(
                _extract_review_patterns(
                    session_id=ctx.session_id,
                    play_id=ctx.play_id,
                    skill_result=self._last_skill_result,
                )
            )
            if records:
                await ctx.store.record_review_patterns(records)
        # Phantom-target backstop (#278): a code_review that fast-failed because
        # the PR doesn't resolve to a real PR on GitHub would otherwise be
        # re-offered every tick — the review-queue row stays pending and any
        # lingering open PR record keeps matching eligibility. Evict it from the
        # mirror so it's offered at most once. #279's confirm-then-write prevents
        # most phantoms at the source; this bounds any that slip through.
        if (
            not outcome.success
            and params.pr_number is not None
            and _is_phantom_pr_error(outcome.error)
        ):
            await ctx.store.mark_pull_request_absent(ctx.session_id, params.pr_number)
            _logger.info(
                "code_review_phantom_target_evicted",
                pr_number=params.pr_number,
                error=outcome.error,
            )
        return outcome


def _extract_review_patterns(
    *,
    session_id: str,
    play_id: int,
    skill_result: SkillResult,
) -> list[ReviewFeedbackPatternRecord]:
    patterns: dict[tuple[str, str], int] = {}
    for item in skill_result.review_patterns:
        _accumulate_pattern(patterns, item)
    for artifact in skill_result.artifacts:
        if not isinstance(artifact, dict):
            continue
        typ = str(artifact.get("type", "")).strip().lower()
        if typ == "review_pattern":
            _accumulate_pattern(patterns, artifact)
            continue
        if typ == "review_patterns":
            raw_items = artifact.get("items")
            if isinstance(raw_items, list):
                for candidate in raw_items:
                    if isinstance(candidate, dict):
                        _accumulate_pattern(patterns, candidate)
    created_at = datetime.now(UTC).isoformat()
    records: list[ReviewFeedbackPatternRecord] = []
    for (pattern, category), frequency in sorted(
        patterns.items(),
        key=lambda item: (-item[1], item[0][0]),
    ):
        records.append(
            ReviewFeedbackPatternRecord(
                session_id=session_id,
                play_id=play_id,
                pattern=pattern,
                category=category,
                frequency=frequency,
                injected=False,
                created_at=created_at,
            )
        )
    return records


def _accumulate_pattern(store: dict[tuple[str, str], int], item: JsonObject) -> None:
    pattern_raw = item.get("pattern")
    if not isinstance(pattern_raw, str):
        return
    pattern = pattern_raw.strip()
    if not pattern:
        return
    category_raw = item.get("category")
    category = category_raw.strip().lower() if isinstance(category_raw, str) else "general"
    if not category:
        category = "general"
    frequency = item.get("frequency", 1)
    if isinstance(frequency, bool):
        frequency = 1
    if isinstance(frequency, str):
        try:
            frequency = int(frequency)
        except ValueError:
            frequency = 1
    if not isinstance(frequency, int):
        frequency = 1
    key = (pattern, category)
    store[key] = store.get(key, 0) + max(1, frequency)
