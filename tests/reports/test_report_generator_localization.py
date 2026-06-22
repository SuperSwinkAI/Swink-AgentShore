"""Tests for ReportGenerator timestamp localization (#255 ESR follow-up)."""

from __future__ import annotations

import time

import pytest

from agentshore.reports.generator import ReportGenerator


@pytest.fixture
def fixed_tz(monkeypatch: pytest.MonkeyPatch):
    """Pin the process tz to a fixed +2h offset for deterministic localization.

    ``Etc/GMT-2`` is UTC+2 (the Etc zones invert the sign), so a 17:52 UTC stamp
    localizes to 19:52.
    """
    monkeypatch.setenv("TZ", "Etc/GMT-2")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_dt_short_localizes_utc(fixed_tz: None) -> None:
    assert ReportGenerator._format_dt_short("2026-06-15T17:52:00+00:00") == "2026-06-15 19:52"


def test_dt_short_localizes_z_suffix(fixed_tz: None) -> None:
    assert ReportGenerator._format_dt_short("2026-06-15T17:52:00Z") == "2026-06-15 19:52"


def test_dt_short_passthrough_on_garbage() -> None:
    # Non-ISO input falls back to the old minute-truncation (first 16 chars).
    assert ReportGenerator._format_dt_short("not-a-real-datetime") == "not-a-real-datet"


def test_dt_short_empty_returns_empty() -> None:
    assert ReportGenerator._format_dt_short(None) == ""
    assert ReportGenerator._format_dt_short("") == ""
