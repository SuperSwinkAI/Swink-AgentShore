"""Every harness has a color + label in each backend map, matching the dashboard.

The dashboard registry (dashboard/src/agentRegistry.ts ``colorFill``) is the
color source of truth; the GitHub ``author:`` label palette and the ESR report
maps mirror it. swink_coding was once missing from all of them (grey label,
fallback-red chart line) — this guard catches the next new agent type.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentshore.core.phases import _AUTHOR_LABEL_COLORS
from agentshore.reports._aggregations import _AGENT_TYPE_LABEL
from agentshore.reports._fleet_concurrency import _HARNESS_COLORS, _HARNESS_LABELS
from agentshore.state import AgentType

_REGISTRY_TS = Path(__file__).parents[1] / "dashboard" / "src" / "agentRegistry.ts"


def _dashboard_colors() -> dict[str, str]:
    text = _REGISTRY_TS.read_text(encoding="utf-8")
    return {
        key: color.upper()
        for key, color in re.findall(
            r'^\s{2}(\w+): \{\s*\n\s*label:[^\n]*\n\s*colorFill: "(#[0-9A-Fa-f]{6})"', text, re.M
        )
    }


@pytest.mark.parametrize("agent_type", list(AgentType), ids=lambda t: t.value)
def test_harness_color_and_label_everywhere(agent_type: AgentType) -> None:
    key = agent_type.value
    dashboard = _dashboard_colors()
    assert key in dashboard, f"{key} missing from agentRegistry.ts"
    color = dashboard[key]
    assert "#" + _AUTHOR_LABEL_COLORS[key].upper() == color
    assert _HARNESS_COLORS[key][0].upper() == color
    assert key in _HARNESS_LABELS
    assert key in _AGENT_TYPE_LABEL
