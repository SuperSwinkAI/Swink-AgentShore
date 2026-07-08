"""Report-time fleet-concurrency aggregation from per-session NDJSON."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from math import ceil
from typing import TYPE_CHECKING, Any

from agentshore.core.concurrency_log import RECORD_VERSION
from agentshore.reports.types import (
    FleetConcurrencyData,
    FleetConcurrencyHistogramEntry,
    FleetConcurrencyPeakEntry,
    FleetConcurrencyRateLimitEntry,
    FleetConcurrencyTierPeakEntry,
    FleetConcurrencyTimelineAxisLabel,
    FleetConcurrencyTimelineData,
    FleetConcurrencyTimelineHarnessEntry,
)

if TYPE_CHECKING:
    from pathlib import Path

_TIMELINE_HEIGHT = 360
# A capacity bar occupies the left gutter (before the y-axis labels), sharing
# the same y-scale/gridlines as the stacked timeline to its right.
_CAPACITY_BAR_LEFT = 34.0
_CAPACITY_BAR_RIGHT = 64.0
_TIMELINE_LEFT = 168.0
_TIMELINE_RIGHT = 1036.0
_TIMELINE_WIDTH = 1096
_TIMELINE_TOP = 52.0
_TIMELINE_BOTTOM = 296.0
_Y_AXIS_LABEL_X = _TIMELINE_LEFT - 34.0
_MAX_TIMELINE_POINTS = 220
_HARNESS_LABELS: dict[str, str] = {
    "antigravity": "Antigravity",
    "claude_code": "Claude Code",
    "codex": "Codex",
    "grok": "Grok",
}

# Keep these in sync with dashboard/src/agentRegistry.ts colorFill so ESR
# timelines match dashboard agent markers/sprites.
_HARNESS_COLORS: dict[str, tuple[str, str]] = {
    "antigravity": ("#4285F4", "rgba(66,133,244,0.22)"),
    "claude_code": ("#E07B39", "rgba(224,123,57,0.24)"),
    "codex": ("#F4D44D", "rgba(244,212,77,0.28)"),
    "grok": ("#14B8A6", "rgba(20,184,166,0.22)"),
}
_FALLBACK_COLORS: tuple[tuple[str, str], ...] = (
    ("#be123c", "rgba(190,18,60,0.18)"),
    ("#4d7c0f", "rgba(77,124,15,0.18)"),
    ("#0f766e", "rgba(15,118,110,0.18)"),
    ("#a16207", "rgba(161,98,7,0.18)"),
)


def collect_fleet_concurrency(
    path: Path,
    session_id: str,
    *,
    tier_config_maxes: dict[str, int] | None = None,
    harness_capacity_maxes: dict[str, int] | None = None,
) -> FleetConcurrencyData | None:
    """Compute ESR fleet-concurrency metrics from a raw NDJSON artifact.

    The file is best-effort observability data. Missing, unreadable, partial, or
    unsupported records are skipped so old or damaged sessions can still render
    an end-session report.
    """
    records = list(_iter_valid_records(path, session_id))
    if not records:
        return None

    busy_values = [record["busy_total"] for record in records]
    sample_count = len(records)
    histogram_counts = Counter(busy_values)

    peak_by_harness: dict[str, int] = {}
    peak_by_harness_tier: dict[str, int] = {}
    rate_limit_samples: list[FleetConcurrencyRateLimitEntry] = []

    for record in records:
        _merge_peaks(peak_by_harness, record["busy_by_type"])
        _merge_peaks(peak_by_harness_tier, record["busy_by_type_tier"])
        if record["completed_error_class"] == "rate_limit":
            rate_limit_samples.append(
                FleetConcurrencyRateLimitEntry(
                    seq=record["seq"],
                    ts=record["ts"],
                    play_type=record["play_type"],
                    completed_agent_type=record["completed_agent_type"],
                    completed_model_tier=record["completed_model_tier"],
                    busy_total=record["busy_total"],
                    busy_by_type=record["busy_by_type"],
                    busy_by_type_tier=record["busy_by_type_tier"],
                )
            )

    return FleetConcurrencyData(
        sample_count=sample_count,
        peak_busy=max(busy_values),
        mean_busy=sum(busy_values) / sample_count,
        peak_by_harness=_peaks_to_rows(peak_by_harness),
        peak_by_harness_tier=_tier_peaks_to_rows(peak_by_harness_tier, tier_config_maxes or {}),
        busy_histogram=[
            FleetConcurrencyHistogramEntry(
                busy_level=busy_level,
                samples=samples,
                sample_share=samples / sample_count,
            )
            for busy_level, samples in sorted(histogram_counts.items())
        ],
        timeline=_build_timeline(records, peak_by_harness, harness_capacity_maxes or {}),
        rate_limit_samples=rate_limit_samples,
    )


def _iter_valid_records(path: Path, session_id: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        record = _coerce_record(raw, session_id)
        if record is not None:
            records.append(record)
    return records


def _coerce_record(raw: object, session_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("v") != RECORD_VERSION or raw.get("session_id") != session_id:
        return None

    busy_total = _coerce_count(raw.get("busy_total"))
    if busy_total is None:
        return None

    return {
        "seq": _coerce_count(raw.get("seq")),
        "ts": _coerce_optional_str(raw.get("ts")),
        "play_type": _coerce_optional_str(raw.get("play_type")),
        "completed_agent_type": _coerce_optional_str(raw.get("completed_agent_type")),
        "completed_model_tier": _coerce_optional_str(raw.get("completed_model_tier")),
        "completed_error_class": _coerce_optional_str(raw.get("completed_error_class")),
        "busy_total": busy_total,
        "busy_by_type": _coerce_counts_map(raw.get("busy_by_type")),
        "busy_by_type_tier": _coerce_counts_map(raw.get("busy_by_type_tier")),
    }


def _coerce_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    if count < 0:
        return None
    return count


def _coerce_counts_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for raw_key, raw_count in value.items():
        if not isinstance(raw_key, str) or not raw_key:
            continue
        count = _coerce_count(raw_count)
        if count is not None:
            result[raw_key] = count
    return result


def _coerce_optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _merge_peaks(peaks: dict[str, int], counts: dict[str, int]) -> None:
    for label, count in counts.items():
        peaks[label] = max(peaks.get(label, 0), count)


def _peaks_to_rows(peaks: dict[str, int]) -> list[FleetConcurrencyPeakEntry]:
    return [
        FleetConcurrencyPeakEntry(label=label, peak_busy=peak)
        for label, peak in sorted(peaks.items())
    ]


def _tier_peaks_to_rows(
    peaks: dict[str, int],
    config_maxes: dict[str, int],
) -> list[FleetConcurrencyTierPeakEntry]:
    return [
        FleetConcurrencyTierPeakEntry(
            label=label,
            peak_busy=peak,
            config_max=config_maxes.get(label),
        )
        for label, peak in sorted(peaks.items())
    ]


def _build_timeline(
    records: list[dict[str, Any]],
    peak_by_harness: dict[str, int],
    harness_capacity: dict[str, int],
) -> FleetConcurrencyTimelineData:
    # Configured-but-idle harnesses (never actually busy this session) still
    # count toward capacity and must appear in the shared ordering, at 0 peak.
    ordering_source = dict(peak_by_harness)
    for harness in harness_capacity:
        ordering_source.setdefault(harness, 0)

    harnesses = [
        label
        for label, _peak in sorted(
            ordering_source.items(),
            key=lambda item: (-item[1], _display_harness_label(item[0])),
        )
    ]
    samples = _downsample_timeline_records(records)
    peak_busy = max((record["busy_total"] for record in records), default=0)
    capacity_total = sum(harness_capacity.get(harness, 0) for harness in harnesses)
    # The shared y-axis always extends to the maximum possible (configured
    # capacity), not just the historical peak, so the capacity bar never clips.
    y_max = max(1, ceil(max(peak_busy, capacity_total) / 5) * 5)

    parsed_start = _parse_ts(records[0]["ts"])
    parsed_end = _parse_ts(records[-1]["ts"])
    duration_seconds = (
        max(0.0, (parsed_end - parsed_start).total_seconds())
        if parsed_start is not None and parsed_end is not None
        else None
    )
    # Place samples at real elapsed offset so idle stretches render as flat gaps;
    # fall back to ordinal spacing only without a usable span.
    use_time_axis = (
        parsed_start is not None and duration_seconds is not None and duration_seconds > 0
    )

    bottoms: dict[str, list[tuple[float, float]]] = {label: [] for label in harnesses}
    tops: dict[str, list[tuple[float, float]]] = {label: [] for label in harnesses}
    total_points: list[tuple[float, float]] = []

    for index, record in enumerate(samples):
        if use_time_axis and parsed_start is not None and duration_seconds is not None:
            sample_ts = _parse_ts(record["ts"])
            elapsed = (sample_ts - parsed_start).total_seconds() if sample_ts is not None else 0.0
            x = _timeline_x(elapsed, duration_seconds)
        else:
            x = _ordinal_x(index, len(samples))
        cumulative = 0
        counts = record["busy_by_type"]
        for harness in harnesses:
            bottoms[harness].append((x, _timeline_y(cumulative, y_max)))
            cumulative += counts.get(harness, 0)
            tops[harness].append((x, _timeline_y(cumulative, y_max)))
        total_points.append((x, _timeline_y(record["busy_total"], y_max)))

    timeline_harnesses: list[FleetConcurrencyTimelineHarnessEntry] = []
    capacity_cumulative = 0
    for index, harness in enumerate(harnesses):
        color, fill = _harness_color(harness, index)
        capacity_max = harness_capacity.get(harness, 0)
        bar_bottom_y = _timeline_y(capacity_cumulative, y_max)
        capacity_cumulative += capacity_max
        bar_top_y = _timeline_y(capacity_cumulative, y_max)
        timeline_harnesses.append(
            FleetConcurrencyTimelineHarnessEntry(
                label=harness,
                display_label=_display_harness_label(harness),
                color=color,
                fill=fill,
                area_points=_points_to_string(tops[harness] + list(reversed(bottoms[harness]))),
                capacity_max=capacity_max,
                capacity_bar_y=bar_top_y,
                capacity_bar_height=max(0.0, bar_bottom_y - bar_top_y),
            )
        )

    return FleetConcurrencyTimelineData(
        width=_TIMELINE_WIDTH,
        height=_TIMELINE_HEIGHT,
        y_axis_labels=_y_axis_labels(y_max),
        x_axis_labels=_x_axis_labels(records, parsed_start, parsed_end, duration_seconds),
        harnesses=timeline_harnesses,
        total_points=_points_to_string(total_points),
        duration_label=_duration_label(records, duration_seconds),
        note="Stacked by harness at completion samples; total busy is overlaid as a line.",
        capacity_total=capacity_total,
        capacity_bar_left=_CAPACITY_BAR_LEFT,
        capacity_bar_right=_CAPACITY_BAR_RIGHT,
    )


def _downsample_timeline_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(records) <= _MAX_TIMELINE_POINTS:
        return records

    result: list[dict[str, Any]] = []
    for bucket_index in range(_MAX_TIMELINE_POINTS):
        start = bucket_index * len(records) // _MAX_TIMELINE_POINTS
        end = (bucket_index + 1) * len(records) // _MAX_TIMELINE_POINTS
        bucket = records[start:end] or [records[min(start, len(records) - 1)]]
        result.append(max(bucket, key=lambda record: record["busy_total"]))
    return result


def _timeline_x(elapsed_seconds: float, span_seconds: float) -> float:
    """Map a sample's real elapsed offset to an x coordinate on the time axis.

    Positioning by wall-clock (not ordinal index) is what makes an idle stretch
    render as a flat gap instead of being smeared across the full width.
    """
    if span_seconds <= 0:
        return _TIMELINE_LEFT
    fraction = min(1.0, max(0.0, elapsed_seconds / span_seconds))
    return _TIMELINE_LEFT + ((_TIMELINE_RIGHT - _TIMELINE_LEFT) * fraction)


def _ordinal_x(index: int, total: int) -> float:
    """Even ordinal spacing — fallback when there is no usable time span."""
    if total <= 1:
        return _TIMELINE_LEFT
    return _TIMELINE_LEFT + ((_TIMELINE_RIGHT - _TIMELINE_LEFT) * index / (total - 1))


def _timeline_y(value: int, y_max: int) -> float:
    return _TIMELINE_BOTTOM - ((_TIMELINE_BOTTOM - _TIMELINE_TOP) * value / y_max)


def _points_to_string(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def _y_axis_labels(y_max: int) -> list[FleetConcurrencyTimelineAxisLabel]:
    step = _nice_axis_step(y_max)
    return [
        FleetConcurrencyTimelineAxisLabel(
            x=_Y_AXIS_LABEL_X,
            y=_timeline_y(value, y_max) + 4.0,
            label=str(value),
        )
        for value in range(0, y_max + 1, step)
    ]


def _nice_axis_step(y_max: int) -> int:
    rough = max(1, ceil(y_max / 5))
    for step in (1, 2, 5, 10, 20, 50):
        if step >= rough:
            return step
    return rough


def _x_axis_labels(
    records: list[dict[str, Any]],
    parsed_start: datetime | None,
    parsed_end: datetime | None,
    duration_seconds: float | None,
) -> list[FleetConcurrencyTimelineAxisLabel]:
    labels: list[FleetConcurrencyTimelineAxisLabel] = []
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = _TIMELINE_LEFT + ((_TIMELINE_RIGHT - _TIMELINE_LEFT) * fraction) - 18.0
        if parsed_start is not None and parsed_end is not None and duration_seconds is not None:
            ts = parsed_start + (parsed_end - parsed_start) * fraction
            # parsed_start/end are tz-aware (UTC); render in the viewer's local tz.
            label = _format_axis_timestamp(ts.astimezone(), duration_seconds)
        else:
            sample_index = round((len(records) - 1) * fraction)
            label = f"#{sample_index + 1}"
        labels.append(FleetConcurrencyTimelineAxisLabel(x=x, y=334.0, label=label))
    return labels


def _format_axis_timestamp(ts: datetime, duration_seconds: float) -> str:
    if duration_seconds <= 36 * 3600:
        return ts.strftime("%H:%M")
    if duration_seconds <= 14 * 24 * 3600:
        return f"{ts.strftime('%b')} {ts.day} {ts.strftime('%H:%M')}"
    return f"{ts.strftime('%b')} {ts.day}"


def _duration_label(records: list[dict[str, Any]], duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return f"{len(records)} completion samples"
    hours = duration_seconds / 3600
    if hours < 48:
        return f"{len(records)} completion samples across {hours:.1f}h"
    days = hours / 24
    return f"{len(records)} completion samples across {days:.1f}d"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _display_harness_label(label: str) -> str:
    if label in _HARNESS_LABELS:
        return _HARNESS_LABELS[label]
    return label.replace("_", " ").title()


def _harness_color(label: str, index: int) -> tuple[str, str]:
    if label in _HARNESS_COLORS:
        return _HARNESS_COLORS[label]
    return _FALLBACK_COLORS[index % len(_FALLBACK_COLORS)]
