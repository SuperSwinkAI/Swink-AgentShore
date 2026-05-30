"""ActivePlayWidget — reactive display of the currently executing play."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from textual.reactive import reactive
from textual.widget import Widget

from agentshore.state import ActivePlay, PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayParams
    from agentshore.state import AgentSnapshot


class ActivePlayWidget(Widget):
    """Shows the active play type and a live elapsed-time counter."""

    play_type: reactive[PlayType | None] = reactive(None, layout=True)
    started_at: reactive[float | None] = reactive(None)
    elapsed_seconds: reactive[int] = reactive(0)
    active_play: reactive[ActivePlay | None] = reactive(None, layout=True)
    _agent_names: dict[str, str] = {}

    def on_mount(self) -> None:
        self.border_title = "Active Play"
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        if self.started_at is not None:
            self.elapsed_seconds = int(time.monotonic() - self.started_at)

    def render(self) -> str:
        if self.play_type is None:
            return "  No active play\n  Waiting for the next policy selection"

        elapsed = _format_elapsed(self.elapsed_seconds)
        if self.active_play is None:
            play_label = f"{_display_play(self.play_type)} ({self.play_type.value})"
            return f"  ▶ {play_label}\n  Elapsed: {elapsed}"

        active = self.active_play
        bits = [f"▶ {_display_play(active.play_type)} ({active.play_type.value})"]
        if active.play_id is not None:
            bits.append(f"play #{active.play_id}")

        lines = ["  " + "  ".join(bits)]
        lines.append(f"  Elapsed: {elapsed}")
        details: list[str] = []
        if active.agent_id:
            details.append(f"agent={self._agent_names.get(active.agent_id, active.agent_id[:12])}")
        if active.issue_number is not None:
            details.append(f"issue=#{active.issue_number}")
        if active.pr_number is not None:
            details.append(f"pr=#{active.pr_number}")
        if active.branch:
            details.append(f"branch={active.branch}")
        if details:
            lines.append("  " + "  ".join(details))
        if active.phase:
            lines.append(f"  phase={active.phase}")
        if active.trigger_error_class:
            lines.append(f"  triggered by {active.trigger_error_class}")
        return "\n".join(lines)

    def set_play(self, play_type: PlayType | None, started_at: float | None) -> None:
        """Set the active play and reset the elapsed counter."""
        self.active_play = None
        self.play_type = play_type
        self.started_at = started_at
        self.elapsed_seconds = 0

    def set_play_started(self, play_type: PlayType, params: PlayParams) -> None:
        """Show a started play event before the next full state snapshot lands."""
        active = ActivePlay(
            play_type=play_type,
            agent_id=params.agent_id,
            started_at=str(params.extras.get("started_at") or datetime.now(UTC).isoformat()),
            play_id=_as_int(params.extras.get("play_id")),
            issue_number=params.issue_number,
            pr_number=params.pr_number,
            branch=params.branch,
        )
        self.update_active_play(active, agents=())

    def update_active_play(
        self,
        active_play: ActivePlay | None,
        *,
        agents: list[AgentSnapshot] | tuple[AgentSnapshot, ...] = (),
    ) -> None:
        """Replace the active-play snapshot with the authoritative state value."""
        self._agent_names = {
            agent.agent_id: agent.display_name or agent.agent_id[:12] for agent in agents
        }
        self.active_play = active_play
        if active_play is None:
            self.play_type = None
            self.started_at = None
            self.elapsed_seconds = 0
            return
        self.play_type = active_play.play_type
        self.started_at = _started_monotonic(active_play.started_at)
        self._tick()


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _started_monotonic(started_at: str) -> float:
    """Convert an ISO timestamp into the monotonic origin used by the ticker."""
    try:
        parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        elapsed = max(0.0, (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())
    except ValueError:
        elapsed = 0.0
    return time.monotonic() - elapsed


def _display_play(play_type: PlayType) -> str:
    return play_type.value.replace("_", " ").title()


def _format_elapsed(seconds: int) -> str:
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours:d}h {mins:02d}m {secs:02d}s"
    return f"[{mins:02d}:{secs:02d}]"
