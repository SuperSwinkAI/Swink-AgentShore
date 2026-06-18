"""Fleet-concurrency ESR aggregation from per-session NDJSON artifacts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentshore.core.concurrency_log import RECORD_VERSION
from agentshore.reports._fleet_concurrency import collect_fleet_concurrency

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
    colors_by_harness = {
        row["label"]: row["color"] for row in data["timeline"]["harnesses"]
    }
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
        row["label"] == "grok" and row["color"] == "#14B8A6"
        for row in timeline["harnesses"]
    )
    assert len(timeline["total_points"].split()) == 220
    assert any("Jun" in row["label"] for row in timeline["x_axis_labels"])
    assert timeline["duration_label"].endswith("d")


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
