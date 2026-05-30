"""Shared alignment level thresholds for TUI displays."""

from __future__ import annotations

ALIGNMENT_HIGH_THRESHOLD = 0.7
ALIGNMENT_MEDIUM_THRESHOLD = 0.3


def alignment_level(ratio: float) -> str:
    if ratio >= ALIGNMENT_HIGH_THRESHOLD:
        return "HIGH"
    if ratio >= ALIGNMENT_MEDIUM_THRESHOLD:
        return "MED"
    return "LOW"
