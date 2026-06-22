"""Fleet-concurrency ESR aggregation from per-session NDJSON artifacts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentshore.core.concurrency_log import RECORD_VERSION
from agentshore.reports._fleet_concurrency import (
    _TIMELINE_BOTTOM,
    _TIMELINE_LEFT,
    _TIMELINE_RIGHT,
    collect_fleet_concurrency,
)
from agentshore.reports.types import FleetConcurrencyTimelineData

SID = "sess-fleet"


def _write_lines(path: Path, records: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        record if isinstance(record, str) else json.dumps(record, separators=(",", ":"))
        for record in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _record(
    seq: int,
    busy_total: int,
    *,
    session_id: str = SID,
    completed_error_class: str | None = None,
    busy_by_type: dict[str, int] | None = None,
    busy_by_type_tier: dict[str, int] | None = None,
    ts: str | None = None,
) -> dict[str, object]:
    return {
        "v": RECORD_VERSION,
        "session_id": session_id,
        "seq": seq,
        "ts": ts or f"2026-06-18T00:0{seq}:00+00:00",
        "play_type": "issue_pickup",
        "completed_agent_type": "claude_code",
        "completed_model_tier": "large",
        "completed_error_class": completed_error_class,
        "busy_total": busy_total,
        "busy_by_type": busy_by_type or {"claude_code": busy_total},
        "busy_by_type_tier": busy_by_type_tier or {"claude_code/large": busy_total},
    }


def test_collects_peak_mean_histogram_and_rate_limit_samples(tmp_path: Path) -> None:
    path = tmp_path / "fleet_concurrency.ndjson"
    _write_lines(
        path,
        [
            _record(
                1,
                1,
                busy_by_type={"claude_code": 1},
                busy_by_type_tier={"claude_code/large": 1},
            ),
            _record(
                2,
                3,
                completed_error_class="rate_limit",
                busy_by_type={"claude_code": 2, "codex": 1},
                busy_by_type_tier={"claude_code/large": 2, "codex/medium": 1},
            ),
            _record(
                3,
                3,
                busy_by_type={"claude_code": 1, "codex": 2},
                busy_by_type_tier={"claude_code/small": 1, "codex/medium": 2},
            ),
        ],
    )

    data = collect_fleet_concurrency(
        path,
        SID,
        tier_config_maxes={"claude_code/large": 2, "codex/medium": 5},
    )

    assert data is not None
    assert data["sample_count"] == 3
    assert data["peak_busy"] == 3
    assert data["mean_busy"] == pytest.approx(7 / 3)
    assert data["peak_by_harness"] == [
        {"label": "claude_code", "peak_busy": 2},
        {"label": "codex", "peak_busy": 2},
    ]
    assert data["peak_by_harness_tier"] == [
        {"label": "claude_code/large", "peak_busy": 2, "config_max": 2},
        {"label": "claude_code/small", "peak_busy": 1, "config_max": None},
        {"label": "codex/medium", "peak_busy": 2, "config_max": 5},
    ]
    assert data["busy_histogram"] == [
        {"busy_level": 1, "samples": 1, "sample_share": pytest.approx(1 / 3)},
        {"busy_level": 3, "samples": 2, "sample_share": pytest.approx(2 / 3)},
    ]
    assert data["rate_limit_samples"] == [
        {
            "seq": 2,
            "ts": "2026-06-18T00:02:00+00:00",
            "play_type": "issue_pickup",
            "completed_agent_type": "claude_code",
            "completed_model_tier": "large",
            "busy_total": 3,
            "busy_by_type": {"claude_code": 2, "codex": 1},
            "busy_by_type_tier": {"claude_code/large": 2, "codex/medium": 1},
        }
    ]
    assert [row["display_label"] for row in data["timeline"]["harnesses"]] == [
        "Claude Code",
        "Codex",
    ]
    assert data["timeline"]["note"] == (
        "Stacked by harness at completion samples; total busy is overlaid as a line."
    )


def test_timeline_uses_dashboard_agent_marker_colors(tmp_path: Path) -> None:
    path = tmp_path / "fleet_concurrency.ndjson"
    _write_lines(
        path,
        [
            _record(
                1,
                15,
                busy_by_type={
                    "antigravity": 1,
                    "claude_code": 2,
                    "codex": 3,
                    "grok": 5,
                },
            )
        ],
    )

    data = collect_fleet_concurrency(path, SID)

    assert data is not None
    colors_by_harness = {row["label"]: row["color"] for row in data["timeline"]["harnesses"]}
    assert colors_by_harness == {
        "antigravity": "#4285F4",
        "claude_code": "#E07B39",
        "codex": "#F4D44D",
        "grok": "#14B8A6",
    }


def test_timeline_supports_grok_random_harness_mix_and_week_scale(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fleet_concurrency.ndjson"
    start = datetime(2026, 6, 1, tzinfo=UTC)
    records = []
    for seq in range(1, 301):
        ts = (start + timedelta(minutes=40 * seq)).isoformat()
        records.append(
            _record(
                seq,
                (seq % 11) + 1,
                ts=ts,
                busy_by_type={
                    "codex": seq % 4,
                    "claude_code": (seq + 1) % 4,
                    "grok": (seq + 2) % 3,
                    "antigravity": (seq + 3) % 2,
                },
                busy_by_type_tier={
                    "codex/medium": seq % 4,
                    "claude_code/large": (seq + 1) % 4,
                    "grok/large": (seq + 2) % 3,
                    "antigravity/medium": (seq + 3) % 2,
                },
            )
        )
    _write_lines(path, records)

    data = collect_fleet_concurrency(path, SID)

    assert data is not None
    timeline = data["timeline"]
    assert {row["display_label"] for row in timeline["harnesses"]} == {
        "Antigravity",
        "Claude Code",
        "Codex",
        "Grok",
    }
    assert any(
        row["label"] == "grok" and row["color"] == "#14B8A6" for row in timeline["harnesses"]
    )
    assert len(timeline["total_points"].split()) == 220
    assert any("Jun" in row["label"] for row in timeline["x_axis_labels"])
    assert timeline["duration_label"].endswith("d")


def _timeline_xs(timeline: FleetConcurrencyTimelineData) -> list[float]:
    return [float(point.split(",")[0]) for point in timeline["total_points"].split()]


def _timeline_ys(timeline: FleetConcurrencyTimelineData) -> list[float]:
    return [float(point.split(",")[1]) for point in timeline["total_points"].split()]


def test_timeline_positions_samples_by_real_time(tmp_path: Path) -> None:
    """Samples are placed at their real elapsed offset, not an even ordinal slot.

    Regression for the smeared busy-timeline (#255 follow-up): a session whose
    activity is front-loaded into the first fraction of its wall-clock must show
    that activity bunched at the left, not stretched across the whole width.
    """
    path = tmp_path / "fleet_concurrency.ndjson"
    start = datetime(2026, 6, 18, tzinfo=UTC)
    records = [
        # Five samples clustered in the first ~4 minutes ...
        _record(seq, 5, ts=(start + timedelta(minutes=seq)).isoformat())
        for seq in range(5)
    ]
    # ... then one lone sample ~100 minutes later (the reap during shutdown).
    records.append(_record(5, 1, ts=(start + timedelta(minutes=100)).isoformat()))
    _write_lines(path, records)

    data = collect_fleet_concurrency(path, SID)

    assert data is not None
    xs = _timeline_xs(data["timeline"])
    span = _TIMELINE_RIGHT - _TIMELINE_LEFT
    left_third = _TIMELINE_LEFT + span * 0.3
    # The early cluster sits in the left ~30% ...
    assert all(x < left_third for x in xs[:5])
    # ... and the late sample is pinned to the far right (NOT evenly spaced).
    assert xs[-1] == pytest.approx(_TIMELINE_RIGHT)


def test_idle_period_renders_flat_gap(tmp_path: Path) -> None:
    """A long idle stretch renders as a wide horizontal gap at the baseline,
    instead of being hidden by even ordinal spacing."""
    path = tmp_path / "fleet_concurrency.ndjson"
    start = datetime(2026, 6, 18, tzinfo=UTC)
    records = [
        _record(0, 3, ts=start.isoformat()),
        _record(1, 5, ts=(start + timedelta(minutes=1)).isoformat()),
        _record(2, 3, ts=(start + timedelta(minutes=2)).isoformat()),
        # Long idle, then two zero-busy reap samples far down the clock.
        _record(
            3, 0, busy_by_type={"claude_code": 0}, ts=(start + timedelta(minutes=120)).isoformat()
        ),
        _record(
            4, 0, busy_by_type={"claude_code": 0}, ts=(start + timedelta(minutes=121)).isoformat()
        ),
    ]
    _write_lines(path, records)

    data = collect_fleet_concurrency(path, SID)

    assert data is not None
    xs = _timeline_xs(data["timeline"])
    ys = _timeline_ys(data["timeline"])
    span = _TIMELINE_RIGHT - _TIMELINE_LEFT
    # Wide gap between the last busy sample and the first idle sample.
    assert xs[3] - xs[2] > span * 0.5
    # Idle samples sit on the zero baseline.
    assert ys[3] == pytest.approx(_TIMELINE_BOTTOM)
    assert ys[4] == pytest.approx(_TIMELINE_BOTTOM)


def test_x_axis_labels_reflect_real_clock_not_ordinal(tmp_path: Path) -> None:
    """The midpoint tick is the wall-clock midpoint (localized), not the median
    sample — even when samples are front-loaded."""
    path = tmp_path / "fleet_concurrency.ndjson"
    start = datetime(2026, 6, 18, tzinfo=UTC)
    end = start + timedelta(hours=2)
    # Front-loaded: many samples in the first 10 minutes ...
    records = [
        _record(seq, 4, ts=(start + timedelta(minutes=seq)).isoformat()) for seq in range(10)
    ]
    # ... and a final sample two hours in, so the span is a clean 2h.
    records.append(_record(10, 1, ts=end.isoformat()))
    _write_lines(path, records)

    data = collect_fleet_concurrency(path, SID)

    assert data is not None
    labels = data["timeline"]["x_axis_labels"]
    assert len(labels) == 5
    # fraction 0.5 → 01:00 UTC, rendered in the machine-local tz.
    expected_mid = (start + timedelta(hours=1)).astimezone().strftime("%H:%M")
    assert labels[2]["label"] == expected_mid


def test_absent_or_empty_file_returns_none(tmp_path: Path) -> None:
    assert collect_fleet_concurrency(tmp_path / "missing.ndjson", SID) is None

    empty = tmp_path / "empty.ndjson"
    empty.write_text("", encoding="utf-8")
    assert collect_fleet_concurrency(empty, SID) is None


def test_invalid_samples_are_ignored_without_raising(tmp_path: Path) -> None:
    path = tmp_path / "fleet_concurrency.ndjson"
    _write_lines(
        path,
        [
            "{not-json",
            ["not", "object"],
            {**_record(1, 1), "v": RECORD_VERSION + 1},
            {**_record(2, 1), "session_id": "other-session"},
            {**_record(3, 1), "busy_total": "not-a-number"},
            _record(4, 2),
        ],
    )

    data = collect_fleet_concurrency(path, SID)

    assert data is not None
    assert data["sample_count"] == 1
    assert data["peak_busy"] == 2
    assert data["busy_histogram"] == [
        {"busy_level": 2, "samples": 1, "sample_share": pytest.approx(1.0)}
    ]
