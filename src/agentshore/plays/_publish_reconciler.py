"""Issue-pickup publish-failure reconciler.

Extracted from ``plays/executor.py`` (TNQA 01 M3).  After an ISSUE_PICKUP run
that passed tests but failed during PR publication (e.g. auth or push error),
this reconciler tries to surface the work product as a PR or branch artifact
so the orchestrator can treat the run as a partial success and avoid re-doing
local work already completed.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
from typing import TYPE_CHECKING

from agentshore.agents.identity import IdentityResolutionError, resolve_identity_env
from agentshore.command import CommandTimeoutError, run_command
from agentshore.error_markers import AUTH_MARKERS
from agentshore.error_markers import PUBLISH_AUTH_MARKERS as _AUTH_ERROR_MARKERS
from agentshore.errors import PreconditionFailed
from agentshore.logging import get_logger
from agentshore.state import PlayOutcome, PlayType

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentshore.agents.manager import AgentManager
    from agentshore.config import RuntimeConfig
    from agentshore.github.adapter import GitHubAdapter
    from agentshore.plays.base import PlayParams
    from agentshore.state import OrchestratorState, SkillResult

_logger = get_logger(__name__)

# _AUTH_ERROR_MARKERS (narrow PUBLISH_AUTH_MARKERS) only scopes the publish-related
# gate below. Auth *classification* uses the broad AUTH_MARKERS, so a publish failure
# with a wider GitHub-auth spelling ("repository not found", …) is still marked auth.

_PR_PUBLISH_ERROR_MARKERS = (
    "pull request",
    "pr creation",
    "create pr",
    "gh pr create",
    "publish",
    "remote branch",
    *_AUTH_ERROR_MARKERS,
)


def _branch_from_artifacts(artifacts: Sequence[object]) -> str | None:
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        branch = artifact.get("branch")
        if isinstance(branch, str) and branch.strip():
            return branch.strip()
    return None


def _pr_number_from_payload(pr: dict[str, object]) -> int | None:
    number = pr.get("number")
    if isinstance(number, bool):
        return None
    if isinstance(number, int):
        return number
    if isinstance(number, str):
        with contextlib.suppress(ValueError):
            return int(number)
    url = str(pr.get("url") or "")
    match = re.search(r"/pull/(\d+)", url)
    return int(match.group(1)) if match else None


def _issue_title(issue_number: int, state: OrchestratorState) -> str | None:
    for issue in state.open_issues:
        if issue.issue_number == issue_number:
            return issue.title.strip() or None
    return None


def _issue_pickup_success_from_pr(
    outcome: PlayOutcome,
    pr: dict[str, object],
    branch: str,
) -> PlayOutcome:
    artifact: dict[str, object] = {
        "type": "pull_request",
        "branch": branch,
    }
    number = _pr_number_from_payload(pr)
    if number is not None:
        artifact["number"] = number
    if pr.get("url"):
        artifact["url"] = str(pr["url"])
    if pr.get("headRefOid"):
        artifact["head_sha"] = str(pr["headRefOid"])
    return dataclasses.replace(
        outcome,
        success=True,
        partial=False,
        error=None,
        artifacts=[artifact, *outcome.artifacts],
    )


def _with_branch_evidence(
    outcome: PlayOutcome,
    branch: str,
    issue_number: int,
) -> PlayOutcome:
    artifact: dict[str, object] = {
        "type": "branch",
        "branch": branch,
        "issue_number": issue_number,
        "publish_reconciliation": "pr_missing_or_auth_failed",
    }
    return dataclasses.replace(outcome, artifacts=[artifact, *outcome.artifacts])


class IssuePickupPublishReconciler:
    """Recover ISSUE_PICKUP runs that passed tests but failed during PR publication.

    Constructed once per ``PlayExecutor`` when a non-None ``GitHubAdapter`` is
    available; the executor delegates its post-dispatch reconciliation step here.
    """

    def __init__(
        self,
        github: GitHubAdapter,
        manager: AgentManager,
        cfg: RuntimeConfig,
        project_path: Path,
    ) -> None:
        self._github = github
        self._manager = manager
        self._cfg = cfg
        self._project_path = project_path

    async def reconcile(
        self,
        play_type: PlayType,
        params: PlayParams,
        outcome: PlayOutcome,
        skill_result: SkillResult,
        state: OrchestratorState,
    ) -> PlayOutcome:
        """Attempt to reconcile a failed publish; return original outcome if not applicable."""
        if play_type != PlayType.ISSUE_PICKUP or outcome.success:
            return outcome
        if skill_result.tests_passed is not True:
            return outcome
        issue_number = skill_result.issue_picked_up or params.issue_number
        branch = skill_result.branch or params.branch or _branch_from_artifacts(outcome.artifacts)
        if issue_number is None or not branch:
            return outcome
        error_text = (outcome.error or skill_result.error or "").lower()
        if not any(marker in error_text for marker in _PR_PUBLISH_ERROR_MARKERS):
            return outcome

        identity_env: dict[str, str] | None = None
        try:
            identity_env = self._identity_env_for_agent(params.agent_id)
        except PreconditionFailed:
            # Assigned agent torn down (e.g. end_agent during wind-down) before this
            # reconcile ran — no handle for an identity overlay. Degrade to branch
            # evidence; next GitHub refresh adopts the branch/PR. Without this,
            # get_handle's PreconditionFailed escaped as play_task_failed, losing
            # completion bookkeeping (#18).
            _logger.info(
                "issue_pickup_publish_reconcile_agent_gone",
                issue_number=issue_number,
                agent_id=params.agent_id,
            )
            return _with_branch_evidence(outcome, branch, issue_number)
        except IdentityResolutionError as exc:
            if params.agent_id is not None:
                await self._manager.mark_agent_error(params.agent_id, "auth", str(exc))
            return _with_branch_evidence(outcome, branch, issue_number)

        pr = await self._github.find_open_pr_by_branch(branch, identity_env=identity_env)
        if pr is not None:
            _logger.info(
                "issue_pickup_publish_reconciled_existing_pr",
                issue_number=issue_number,
                branch=branch,
                pr_number=pr.get("number"),
            )
            return _issue_pickup_success_from_pr(outcome, pr, branch)

        if await self._remote_branch_exists(branch):
            issue_title = _issue_title(issue_number, state)
            # Honor configured project.target_branch; fall back to the repo's GitHub
            # default when unset (preserves pre-field projects). See desktop-53m0.
            base = self._cfg.project.target_branch or await self._github.default_branch(
                identity_env=identity_env
            )
            title = f"Fix #{issue_number}: {issue_title}" if issue_title else f"Fix #{issue_number}"
            pr = await self._github.create_pr(
                title=title,
                body=(
                    f"Closes #{issue_number}\n\n"
                    "AgentShore created this PR after the assigned agent completed local work "
                    "and tests but failed during PR publication."
                ),
                head=branch,
                base=base,
                idempotency_key=f"issue_pickup_reconcile:{issue_number}:{branch}",
                identity_env=identity_env,
            )
            if pr is not None:
                _logger.info(
                    "issue_pickup_publish_reconciled_created_pr",
                    issue_number=issue_number,
                    branch=branch,
                    pr_number=pr.get("number"),
                )
                return _issue_pickup_success_from_pr(outcome, pr, branch)

        if any(marker in error_text for marker in AUTH_MARKERS) and params.agent_id:
            await self._manager.mark_agent_error(
                params.agent_id,
                "auth",
                outcome.error or "issue_pickup PR publish failed due to GitHub auth",
            )
        return _with_branch_evidence(outcome, branch, issue_number)

    def _identity_env_for_agent(self, agent_id: str | None) -> dict[str, str]:
        if agent_id is None:
            raise IdentityResolutionError("no assigned agent for identity overlay")
        handle = self._manager.get_handle(agent_id)
        agent_cfg = self._cfg.agents.get(handle.agent_type.value)
        if agent_cfg is None:
            return {}
        return resolve_identity_env(self._cfg, agent_cfg, strict=True)

    async def _remote_branch_exists(self, branch: str) -> bool:
        try:
            result = await run_command(
                "git",
                "ls-remote",
                "--exit-code",
                "--heads",
                "origin",
                f"refs/heads/{branch}",
                cwd=self._project_path,
                stdout=asyncio.subprocess.DEVNULL,
                timeout_seconds=30,
            )
        except (OSError, CommandTimeoutError) as exc:
            _logger.warning("issue_pickup_branch_check_failed", branch=branch, error=str(exc))
            return False
        if result.returncode == 0:
            return True
        if result.stderr:
            _logger.debug(
                "issue_pickup_remote_branch_missing",
                branch=branch,
                stderr=result.stderr[:300],
            )
        return False
