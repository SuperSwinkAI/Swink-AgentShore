"""PlayHistoryTable widget — scrollable DataTable of completed play outcomes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

from textual.widgets import DataTable

if TYPE_CHECKING:
    from agentshore.state import PlayOutcome

DEFAULT_VISIBLE_ROW_LIMIT = 5
NARROW_VISIBLE_ROW_LIMIT = 3


class _PlayHistoryTableInitKwargs(TypedDict, total=False):
    show_header: bool
    show_row_labels: bool
    fixed_rows: int
    fixed_columns: int
    zebra_stripes: bool
    header_height: int
    show_cursor: bool
    cursor_foreground_priority: Literal["renderable", "css"]
    cursor_background_priority: Literal["renderable", "css"]
    cursor_type: Literal["cell", "row", "column", "none"]
    cell_padding: int
    name: str | None
    id: str | None
    classes: str | None
    disabled: bool


class PlayHistoryTable(DataTable[str]):
    """Table that shows the most recent completed play outcomes."""

    visible_row_limit: int
    _history_rows: list[tuple[str, ...]]

    def __init__(self, **kwargs: Unpack[_PlayHistoryTableInitKwargs]) -> None:
        super().__init__(**kwargs)
        self.visible_row_limit = DEFAULT_VISIBLE_ROW_LIMIT
        self._history_rows = []

    def on_mount(self) -> None:
        self._sync_border_title()
        self.add_columns("ID", "Play", "Result", "Δ", "Cost", "Duration", "Message")
        self.cursor_type = "row"
        self.zebra_stripes = True

    def add_play_row(self, outcome: PlayOutcome, agent_display_name: str | None = None) -> None:
        """Append a completed play outcome as a new table row."""
        success_icon = "✓" if outcome.success else "✗"
        if outcome.partial:
            success_icon = f"{success_icon}/partial"
        elif outcome.error:
            success_icon = f"{success_icon}/error"
        cost_str = f"${outcome.dollar_cost:.3f}"
        dur_str = f"{outcome.duration_seconds:.1f}s"
        delta = "n/a" if outcome.alignment_delta is None else f"{outcome.alignment_delta:+.2f}"
        play_id = str(outcome.play_id) if outcome.play_id is not None else ""
        msg = ""
        if outcome.error:
            msg = outcome.error[:60]
        elif outcome.skipped and outcome.skip_category is not None:
            msg = str(outcome.skip_category)
        self._history_rows.append(
            (
                play_id,
                outcome.play_type.value,
                success_icon,
                delta,
                cost_str,
                dur_str,
                msg,
            )
        )
        self._rebuild_visible_rows()

    def set_visible_row_limit(self, limit: int) -> None:
        """Set how many recent play rows are displayed in the dashboard."""
        self.visible_row_limit = max(1, limit)
        self._sync_border_title()
        self._rebuild_visible_rows()

    def _sync_border_title(self) -> None:
        self.border_title = f"Recent Plays (last {self.visible_row_limit})"

    def _rebuild_visible_rows(self) -> None:
        self.clear(columns=False)
        start = max(0, len(self._history_rows) - self.visible_row_limit)
        for index, cells in enumerate(self._history_rows[start:], start=start):
            self.add_row(*cells, key=f"play-history-{index}")
