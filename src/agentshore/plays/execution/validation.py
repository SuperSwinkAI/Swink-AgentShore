"""Anti-confirmation identity check for play dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.errors import PreconditionFailed
from agentshore.identity_names import same_identity
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.agents.manager import AgentManager
    from agentshore.data.store import DataStore
    from agentshore.plays.base import PlayParams


async def _anti_confirmation_check(
    manager: AgentManager,
    store: DataStore,
    session_id: str,
    play_type: PlayType,
    params: PlayParams,
    candidate_agent_id: str,
) -> str | None:
    """Return an error string if the candidate violates self-review.

    CODE_REVIEW is the only play with an anti-confirmation invariant:
    the reviewer's GitHub identity must differ from the PR author's
    GitHub login. Every other play (including RUN_QA, which exercises
    the merged trunk) accepts any qualified agent.

    Identity is the only deconfliction key. Agent type plays no role —
    a human and an agent can share a GH login, and two agents of the
    same type can have different logins. The resolver pre-filters to a
    cross-identity reviewer; this check is defense-in-depth against
    races (handle reassignment between resolve and dispatch).
    """
    if play_type != PlayType.CODE_REVIEW or params.pr_number is None:
        return None

    try:
        handle = manager.get_handle(candidate_agent_id)
    except (PreconditionFailed, KeyError):
        return None
    candidate_identity = handle.github_identity
    if candidate_identity is None:
        return None

    pr_author = await store.get_pr_github_author(params.pr_number, session_id)
    if pr_author is None:
        return None

    if same_identity(candidate_identity, pr_author):
        return (
            f"anti_confirmation_violation: agent {candidate_agent_id!r} "
            f"identity {candidate_identity!r} authored PR #{params.pr_number}"
        )
    return None
