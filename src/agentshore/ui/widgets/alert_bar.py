"""AlertBar widget — hidden-by-default banner for error / warning / info messages."""

from __future__ import annotations

from textual.widget import Widget


class AlertBar(Widget):
    """Displays a dismissible alert banner; hidden until ``show()`` is called."""

    DEFAULT_CSS = "AlertBar { display: none; height: 3; }"

    _message: str = ""
    _level: str = "info"

    def render(self) -> str:
        if self._level == "loop":
            return f"  LOOP DETECTED — {self._message} — [R]evert [O]verride [Q]uit"
        icon = {"error": "✗", "warning": "⚠", "info": "ℹ"}.get(self._level, "ℹ")
        return f"  {icon} {self._message}"

    def show(self, message: str, level: str = "info") -> None:
        """Make the bar visible with *message* styled according to *level*."""
        self._message = message
        self._level = level
        self.display = True
        self.remove_class("alert--info", "alert--warning", "alert--error", "alert--loop")
        self.add_class(f"alert--{level}")
        self.refresh()

    def show_loop(self, play_name: str, streak: int) -> None:
        """Show a full-width loop escalation alert with action affordances."""
        self._message = f"{play_name} failed {streak}x"
        self._level = "loop"
        self.display = True
        self.remove_class("alert--info", "alert--warning", "alert--error")
        self.add_class("alert--loop")
        self.refresh()

    def hide(self) -> None:
        """Hide the bar."""
        self.display = False
        self.remove_class("alert--loop")
        self.refresh()
