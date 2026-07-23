"""AgentHandle dataclass — per-agent runtime state and lifecycle transitions."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agentshore.agents.registry import AGENT_TYPE_DISPLAY_PREFIX
from agentshore.logging import get_logger

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from agentshore.errors import ErrorClass
    from agentshore.state import AgentStatus, AgentType, PlayType

_logger = get_logger(__name__)

_ADJECTIVES = (
    "Quantum",
    "Stellar",
    "Cyber",
    "Nano",
    "Hyper",
    "Plasma",
    "Photon",
    "Warp",
    "Chrono",
    "Orbital",
    "Void",
    "Flux",
    "Ionic",
    "Nova",
    "Spectral",
    "Fusion",
    "Tachyon",
    "Galactic",
    "Nebular",
    "Astral",
    "Pulsar",
    "Gravitic",
    "Thermal",
    "Vector",
    "Binary",
    "Radiant",
    "Kinetic",
    "Phantom",
    "Sonic",
    "Cryo",
    "Cobalt",
    "Xenon",
    "Arcane",
    "Ember",
    "Rogue",
    "Prime",
    "Silent",
    "Obsidian",
    "Crimson",
    "Apex",
    "Iron",
    "Stealth",
    "Dark",
    "Zero",
    "Omega",
    "Alpha",
    "Delta",
    "Neon",
    "Lucid",
    "Titan",
)
_NOUNS = (
    "Sentinel",
    "Nexus",
    "Cipher",
    "Vortex",
    "Beacon",
    "Wraith",
    "Specter",
    "Droid",
    "Cortex",
    "Matrix",
    "Relay",
    "Phantom",
    "Vector",
    "Probe",
    "Shard",
    "Circuit",
    "Reactor",
    "Synth",
    "Forge",
    "Node",
    "Pulse",
    "Helix",
    "Prism",
    "Comet",
    "Orbit",
    "Core",
    "Spark",
    "Bolt",
    "Ghost",
    "Blade",
    "Striker",
    "Runner",
    "Weaver",
    "Seeker",
    "Hunter",
    "Phoenix",
    "Raptor",
    "Titan",
    "Aegis",
    "Nomad",
    "Reaper",
    "Falcon",
    "Serpent",
    "Raven",
    "Storm",
    "Flare",
    "Drone",
    "Atlas",
    "Zenith",
    "Onyx",
)


def _generate_display_name(agent_type: AgentType, model_tier: str | None = None) -> str:
    """Generate a memorable display name like 'Claude: Fiery Robot'."""
    prefix = AGENT_TYPE_DISPLAY_PREFIX.get(agent_type, "Agent")
    if model_tier:
        prefix = f"{prefix}/{model_tier}"
    adj = secrets.choice(_ADJECTIVES)
    noun = secrets.choice(_NOUNS)
    return f"{prefix}: {adj} {noun}"


@dataclass(frozen=True, slots=True)
class AgentInvocationResult:
    """Raw output and metadata returned by a single adapter dispatch."""

    raw_output: str
    tokens_in: int
    tokens_out: int
    dollar_cost: float
    duration_ms: int
    exit_code: int
    cached_tokens_in: int = 0
    cache_write_tokens_in: int = 0
    turn_count: int = 0
    max_turn_input_tokens: int = 0
    session_id: str | None = None


def is_noop_invocation(result: AgentInvocationResult) -> bool:
    """True when a dispatch was a clean-exit empty no-op.

    The agent process exited 0 (no crash, no kill) yet produced no usable
    output at all — ``raw_output`` is empty after the adapter's own unwrapping
    (e.g. agy's ``(empty)`` task envelope is already flattened to ``""`` by
    ``cli_antigravity.extract_output`` before the result reaches here). This is
    the agy empty-no-op signature; codex/grok empty output arrives with a
    non-zero exit or a kill and so never matches. The single shared definition
    used by both the manager (telemetry) and the no-op retry (skill_backed/base).
    """
    return result.exit_code == 0 and not result.raw_output.strip()


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """Record of one dispatched task appended to an AgentHandle's history."""

    play_id: str
    play_type: PlayType
    success: bool
    branch: str | None = None


@dataclass(slots=True)
class AgentHandle:
    """Runtime container for a single managed agent.

    Status transitions are always made through ``transition_to()`` so that
    structlog events are emitted consistently and ``last_active`` is kept
    up-to-date.
    """

    agent_id: str
    agent_type: AgentType
    status: AgentStatus
    working_dir: Path
    display_name: str = ""
    model: str | None = None
    model_tier: str | None = None
    reasoning_effort: str | None = None
    dispatches: int = 0
    context_size: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_active: datetime | None = None
    task_history: list[TaskRecord] = field(default_factory=list)
    # subprocess reference for CLI agents; not included in repr to avoid noise
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    # Monotonic-clock deadline by which the current dispatch must have finished
    # (effective wall-clock timeout + kill grace + margin), stamped by
    # ``AgentManager.dispatch`` at the BUSY transition. The HealthMonitor reaps
    # any agent still BUSY past this deadline — the catch-all for a dispatch
    # whose own timeout/kill machinery hung and left the agent pinned in BUSY
    # (which would otherwise suppress every session-end backstop). Only read
    # while ``status is BUSY``; a stale value on an idle agent is ignored.
    dispatch_deadline_monotonic: float | None = field(default=None, repr=False)
    current_play_type: PlayType | None = None
    current_play_id: int | None = None
    current_play_started_at: str | None = None
    current_play_issue_number: int | None = None
    current_play_pr_number: int | None = None
    current_play_branch: str | None = None
    last_error_class: ErrorClass | None = None
    timeout_count: int = 0
    # Timeouts since this agent's last *successful* dispatch (reset to 0 on
    # success). Distinct from the cumulative ``timeout_count`` telemetry: this is
    # the consecutive-storm signal. A hung agent that produces no stdout returns
    # to IDLE and — if it has prior completions — is not benched by the
    # 0-completion circuit breaker, so reconcile_state re-selects it and it hangs
    # for the full stream-idle window again (#161). Benching on this counter
    # bounds the storm without disabling the self-heal play itself.
    consecutive_timeouts: int = 0
    # Cumulative count of clean-exit empty no-op dispatches this agent returned
    # (see is_noop_invocation). Telemetry/agent-health only — never reset — so a
    # session report can show an agent's no-op rate instead of those dispatches
    # showing up only as silent play failures. The consecutive-no-op streak that
    # triggers a take_break is bounded by the in-play retry loop, not this field.
    noop_count: int = 0
    github_identity: str | None = None
    # Identity env overlay resolved once at instantiate() and reused by every
    # dispatch — never re-resolved per play. Empty when the agent has no bound
    # identity. Treat as read-only; dispatch builds a per-call copy before adding
    # transient keys like ``AGENTSHORE_PROJECT_PATH``.
    identity_env: dict[str, str] = field(default_factory=dict)

    def transition_to(self, new_status: AgentStatus) -> None:
        """Change status and emit a structlog INFO event."""
        old_status = self.status
        self.status = new_status
        self.last_active = datetime.now(UTC)
        _logger.info(
            "agent_status_changed",
            agent_id=self.agent_id,
            from_status=old_status.value,
            to_status=new_status.value,
        )

    def add_task(self, record: TaskRecord) -> None:
        """Append a completed task to history."""
        self.task_history.append(record)

    def start_play(
        self,
        *,
        play_type: PlayType,
        play_id: int,
        started_at: str,
        issue_number: int | None,
        pr_number: int | None,
        branch: str | None,
    ) -> None:
        """Record the current play for state snapshots while the agent works."""
        self.current_play_type = play_type
        self.current_play_id = play_id
        self.current_play_started_at = started_at
        self.current_play_issue_number = issue_number
        self.current_play_pr_number = pr_number
        self.current_play_branch = branch

    def clear_play(self, play_id: int | None = None) -> None:
        """Clear the current play, optionally only if it matches *play_id*."""
        if play_id is not None and self.current_play_id != play_id:
            return
        self.current_play_type = None
        self.current_play_id = None
        self.current_play_started_at = None
        self.current_play_issue_number = None
        self.current_play_pr_number = None
        self.current_play_branch = None

    def accumulate(self, *, tokens_in: int, tokens_out: int, dollar_cost: float) -> None:
        """Add token and cost counters from a finished invocation."""
        self.total_tokens += tokens_in + tokens_out
        self.total_cost += dollar_cost
        self.last_active = datetime.now(UTC)

    def snapshot_dict(self) -> dict[str, object]:
        """Minimal dict for logging/debugging without circular refs."""
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type.value,
            "model": self.model,
            "model_tier": self.model_tier,
            "reasoning_effort": self.reasoning_effort,
            "status": self.status.value,
            "context_size": self.context_size,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "current_play_type": (
                self.current_play_type.value if self.current_play_type is not None else None
            ),
            "current_play_id": self.current_play_id,
            "noop_count": self.noop_count,
        }
