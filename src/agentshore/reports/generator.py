"""Report generator — reads SQLite, renders Jinja2 templates to self-contained HTML."""

from __future__ import annotations

import asyncio
import importlib.resources
import webbrowser
from datetime import datetime
from typing import TYPE_CHECKING

import jinja2

from agentshore.reports.collector import ReportDataCollector

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.data.store import DataStore


class ReportGenerator:
    """Generates self-contained HTML reports from session data."""

    def __init__(self, store: DataStore) -> None:
        self._store = store
        self._collector = ReportDataCollector(store)
        self._env = self._create_jinja_env()

    def _create_jinja_env(self) -> jinja2.Environment:
        """Create Jinja2 environment with templates loaded from the package."""
        env = jinja2.Environment(
            loader=jinja2.PackageLoader("agentshore.reports", "templates"),
            autoescape=jinja2.select_autoescape(["html"]),
        )
        env.filters["duration"] = self._format_duration
        env.filters["dt_short"] = self._format_dt_short
        return env

    def _load_chartjs(self) -> str:
        """Load the vendored Chart.js source for inline embedding."""
        static = importlib.resources.files("agentshore.reports") / "static"
        return (static / "chart.min.js").read_text(encoding="utf-8")

    @staticmethod
    def _format_dt_short(value: str | None) -> str:
        """Localize an ISO datetime to the viewer's tz, minute precision: 2026-06-15 17:52.

        Stored timestamps are UTC; the report is a static file generated and read
        on the same machine, so rendering in the machine-local tz at generation
        time gives the viewer local wall-clock without any browser JS. Falls back
        to the raw minute-truncated string if the value isn't parseable ISO.
        """
        if not value:
            return value or ""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value[:16]
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()  # no-arg astimezone() -> machine-local tz
        return parsed.strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(round(seconds)))
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes}m {secs}s"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    async def generate_session_summary(
        self,
        session_id: str,
        output_dir: Path,
        *,
        open_browser: bool = False,
    ) -> Path:
        """Generate a full session summary report as a self-contained HTML file.

        Parameters
        ----------
        session_id:
            The session to report on.
        output_dir:
            Directory where the HTML file will be written.
        open_browser:
            If *True*, open the report in the default browser.

        Returns
        -------
        Path to the generated HTML file.
        """
        data = await self._collector.collect_session_summary(session_id)
        template = self._env.get_template("session_summary.html.j2")
        html = template.render(
            data=data,
            chart_js=self._load_chartjs(),
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"session-{session_id[:8]}-summary.html"
        await asyncio.to_thread(path.write_text, html, encoding="utf-8")
        if open_browser:
            await asyncio.to_thread(webbrowser.open, path.resolve().as_uri())
        return path

    async def generate_end_session_report(
        self,
        session_id: str,
        output_dir: Path,
        *,
        open_browser: bool = False,
    ) -> Path:
        """Generate the compact static end-of-session report."""
        data = await self._collector.collect_end_session_report(session_id)
        template = self._env.get_template("end_session_report.html.j2")
        html = template.render(data=data)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"end-session-{session_id}.html"
        await asyncio.to_thread(path.write_text, html, encoding="utf-8")
        if open_browser:
            await asyncio.to_thread(webbrowser.open, path.resolve().as_uri())
        return path

    async def generate_progress_report(
        self,
        session_id: str,
        output_dir: Path,
    ) -> Path:
        """Generate a mid-session progress report.

        Parameters
        ----------
        session_id:
            The session to report on.
        output_dir:
            Directory where the HTML file will be written.

        Returns
        -------
        Path to the generated HTML file.
        """
        data = await self._collector.collect_progress_report(session_id)
        template = self._env.get_template("progress_report.html.j2")
        html = template.render(data=data, chart_js=self._load_chartjs())
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"session-{session_id[:8]}-progress.html"
        await asyncio.to_thread(path.write_text, html, encoding="utf-8")
        return path

    async def generate_comparison(
        self,
        id1: str,
        id2: str,
        output_dir: Path,
    ) -> Path:
        """Generate a side-by-side comparison of two sessions.

        Parameters
        ----------
        id1:
            First session ID.
        id2:
            Second session ID.
        output_dir:
            Directory where the HTML file will be written.

        Returns
        -------
        Path to the generated HTML file.
        """
        data = await self._collector.collect_comparison(id1, id2)
        template = self._env.get_template("archive_comparison.html.j2")
        html = template.render(data=data, chart_js=self._load_chartjs())
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"comparison-{id1[:8]}-vs-{id2[:8]}.html"
        await asyncio.to_thread(path.write_text, html, encoding="utf-8")
        return path
