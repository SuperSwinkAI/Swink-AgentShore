"""InternalPlay — base class for agentless, non-skill-backed plays.

Internal plays wrap ``AgentManager`` lifecycle operations (instantiate, end
agent, end session, take break) or reserve action-space slots. None of them
dispatch a skill, so they all share the same trivial protocol surface:
``skill_name`` and ``capability`` are always ``None`` and the default
``estimated_cost`` is free. Subclasses set ``play_type`` and implement
``preconditions``/``execute``; plays with a real dollar cost (instantiate,
take_break) override ``estimated_cost``.

Deliberately not a subclass of the ``Play`` protocol: internal plays satisfy
``Play`` structurally (the registry type-checks against the protocol), and
``Play`` declares ``play_type`` as a property whereas internal plays set it as a
plain class attribute. Keeping ``InternalPlay`` a standalone ABC avoids a
property-vs-classvar override clash while still sharing the boilerplate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.rl.mask_reason import MaskReason
    from agentshore.state import OrchestratorState, PlayOutcome, PlayType


class InternalPlay(ABC):
    """Agentless play that wraps an AgentManager operation or reserves a slot.

    Defaults ``skill_name``/``capability`` to ``None`` (no skill dispatch) and
    ``estimated_cost`` to ``0.0``. Subclasses set the ``play_type`` class
    attribute and implement ``preconditions`` and ``execute``.
    """

    play_type: PlayType
    skill_name: str | None = None
    capability: str | None = None

    # Declarative executor-behavior flags (see ``Play`` for semantics). Inert by
    # default; ``end_agent`` overrides ``is_handoff``.
    authors_prs: bool = False
    retarget_pr_base: bool = False
    is_handoff: bool = False
    is_observation: bool = False
    requeue_on_anti_confirmation: bool = False

    def estimated_cost(self, state: OrchestratorState) -> float:
        return 0.0

    @abstractmethod
    def preconditions(self, state: OrchestratorState) -> list[MaskReason]: ...

    @abstractmethod
    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome: ...
