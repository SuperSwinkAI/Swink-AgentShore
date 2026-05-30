"""AgentPanel widget — reactive display of agent status rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from textual.reactive import reactive
from textual.widget import Widget

if TYPE_CHECKING:
    from agentshore.plays.base import PlayParams
    from agentshore.state import ActivePlay, AgentSnapshot, PlayType

MAX_VISIBLE_AGENTS = 10


class AgentPanel(Widget):
    """Displays active agents and their current plays in a dense table."""

    agents: reactive[list[AgentSnapshot]] = reactive([], layout=True)
    active_play: reactive[ActivePlay | None] = reactive(None, layout=True)

    def on_mount(self) -> None:
        self.border_title = f"Agents & Active Plays (up to {MAX_VISIBLE_AGENTS})"
        self.set_interval(1.0, self.refresh)

    def render(self) -> str:
        if not self.agents:
            return "  No agents\n  Instantiate an agent to start work"
        header = (
            "  Agent              State       Play                 Target      "
            "Elapsed  Ctx     Cost"
        )
        lines = [
            header,
            "  " + "─" * 84,
        ]
        status_symbols: dict[str, str] = {
            "idle": "●",
            "busy": "◉",
            "error": "✕",
            "terminated": "—",
        }
        for agent in self.agents[:MAX_VISIBLE_AGENTS]:
            sym = status_symbols.get(agent.status.value, "?")
            play = _agent_play(agent, self.active_play)
            name = _truncate(agent.display_name or agent.agent_id, 16)
            lines.append(
                f"  {sym} {name:<16} {agent.status.value.upper():<10} "
                f"{_truncate(play.play_label, 20):<20} "
                f"{_truncate(play.target, 10):<10} "
                f"{play.elapsed:<8} "
                f"{_compact_count(agent.context_size):>6} "
                f"${agent.total_cost:.3f}"
            )
        if len(self.agents) > MAX_VISIBLE_AGENTS:
            lines.append(f"  +{len(self.agents) - MAX_VISIBLE_AGENTS} more agents")
        return "\n".join(lines)

    def update_agents(
        self,
        agents: list[AgentSnapshot],
        active_play: ActivePlay | None = None,
    ) -> None:
        """Replace the displayed agent list with a fresh snapshot."""
        self.agents = list(agents)
        self.active_play = active_play

    def set_play_started(self, play_type: PlayType, params: PlayParams) -> None:
        """Show a play-start hint before the next full state snapshot lands."""
        from agentshore.state import ActivePlay

        self.active_play = ActivePlay(
            play_type=play_type,
            agent_id=params.agent_id,
            started_at=str(params.extras.get("started_at") or datetime.now(UTC).isoformat()),
            play_id=_as_int(params.extras.get("play_id")),
            issue_number=params.issue_number,
            pr_number=params.pr_number,
            branch=params.branch,
        )

    def clear_active_play(self) -> None:
        """Clear the play-start hint after a completion event."""
        self.active_play = None


class _AgentPlay:
    def __init__(self, play_label: str, target: str, elapsed: str) -> None:
        self.play_label = play_label
        self.target = target
        self.elapsed = elapsed


def _agent_play(agent: AgentSnapshot, active_play: ActivePlay | None) -> _AgentPlay:
    if agent.current_play_type is not None:
        return _AgentPlay(
            agent.current_play_type.value,
            _target_label(
                issue_number=agent.current_play_issue_number,
                pr_number=agent.current_play_pr_number,
                branch=agent.current_play_branch,
            ),
            _elapsed_label(agent.current_play_started_at),
        )
    if active_play is not None and active_play.agent_id == agent.agent_id:
        return _AgentPlay(
            active_play.play_type.value,
            _target_label(
                issue_number=active_play.issue_number,
                pr_number=active_play.pr_number,
                branch=active_play.branch,
            ),
            _elapsed_label(active_play.started_at),
        )
    return _AgentPlay("—", "—", "—")


def _target_label(
    *,
    issue_number: int | None,
    pr_number: int | None,
    branch: str | None,
) -> str:
    if issue_number is not None:
        return f"#{issue_number}"
    if pr_number is not None:
        return f"PR #{pr_number}"
    if branch:
        return branch
    return "—"


def _elapsed_label(started_at: str | None) -> str:
    if not started_at:
        return "—"
    try:
        parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        seconds = max(0, int((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()))
    except ValueError:
        return "—"
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}h{mins:02d}m"
    return f"{mins:02d}:{secs:02d}"


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _compact_count(value: int) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
