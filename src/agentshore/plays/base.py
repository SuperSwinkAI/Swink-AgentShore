"""Play protocol, PlayParams, and PlayExecutionContext."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, Union, runtime_checkable

from agentshore.rl.mask_reason import MaskReason
from agentshore.state import OrchestratorState, PlayOutcome, PlayType

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.agents.manager import AgentManager
    from agentshore.agents.worktree import TrunkAllocation, WorktreeAllocation
    from agentshore.config import RuntimeConfig
    from agentshore.data.store import DataStore
    from agentshore.state import StateProvider

# Runtime-only handle stamped by the dispatcher; never JSON-serialized.
# Quoted so the forward reference works without importing the worktree
# package at module load (the import would form a cycle through manager.py).
_RuntimeAllocation = Union["WorktreeAllocation", "TrunkAllocation", None]


@dataclass(frozen=True, slots=True)
class PlayParams:
    """Resolved parameters for a play execution.

    The executor populates this from the ParameterResolver's output and any
    human overrides supplied via CLI or API.

    ``_runtime_allocation`` (issue #563 follow-up): the dispatcher stamps a
    ``WorktreeAllocation`` / ``TrunkAllocation`` here for the executor's
    finalize path. It is deliberately NOT in ``extras`` — extras crosses
    the JSON boundary (context.json + dispatch_replay rows), and shipping
    live Python dataclass handles through that surface is what produced
    the TrunkAllocation/Path/PlayType onion bug. ``_runtime_allocation``
    is private (leading underscore), excluded from ``repr`` so it doesn't
    pollute logs, and excluded from ``compare`` so two params identical
    in every other way still compare equal regardless of allocation
    identity. ``params_to_json_safe_dict`` omits it.
    """

    agent_id: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    branch: str | None = None
    num_commits: int | None = None
    url: str | None = None
    seed_path: str | None = None
    scope: str | None = None
    target_agent_type: str | None = None
    target_model_tier: str | None = None
    source_agent_id: str | None = None
    target_agent_id: str | None = None
    reason: str | None = None
    # Trusted internal queueing (bootstrap fleet seeding) sets this so the
    # override path skips the action mask. Never set this from policy code.
    bypass_preconditions: bool = False
    extras: dict[str, object] = field(default_factory=dict)
    _runtime_allocation: _RuntimeAllocation = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class PlayExecutionContext:
    """Services injected by the executor into each play.execute() call.

    Gives plays access to Phase 1 services (manager, store, cfg) and the
    current play_id (needed for FK-constrained writes like agent_handoffs and
    scope_drift_log) without importing the Orchestrator (avoids circular deps).
    """

    session_id: str
    play_id: int
    manager: AgentManager
    store: DataStore
    cfg: RuntimeConfig
    project_path: Path
    state_provider: StateProvider | None = None


@runtime_checkable
class Play(Protocol):
    """Contract every play class must satisfy.

    ``preconditions`` returns a list of unmet condition descriptions — an empty
    list means the play may execute.  The action mask for the RL engine is built
    by calling preconditions on every play and masking out those with non-empty
    results.

    ``estimated_cost`` gives the play executor a dollar projection for budget
    gating before dispatch.

    ``execute`` runs the play and returns a PlayOutcome.  It must never raise
    (catch all exceptions and embed them in the outcome).
    """

    @property
    def play_type(self) -> PlayType: ...

    @property
    def skill_name(self) -> str | None: ...

    @property
    def capability(self) -> str | None: ...

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]: ...

    def estimated_cost(self, state: OrchestratorState) -> float: ...

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome: ...
