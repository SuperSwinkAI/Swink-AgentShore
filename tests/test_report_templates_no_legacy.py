"""Regression guard: report templates must not carry the legacy project name.

The ESR header/title/footer rendered "Foreman" after the rebrand; this asserts
zero legacy refs across the report templates and that the ESR identifies as
AgentShore.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATE_ROOT = Path(__file__).parent.parent / "src" / "agentshore" / "reports" / "templates"


def _templates() -> list[Path]:
    return sorted(_TEMPLATE_ROOT.glob("*.j2"))


def test_templates_exist() -> None:
    assert _templates(), "expected report templates under reports/templates"


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_no_legacy_foreman_in_template(template: Path) -> None:
    text = template.read_text(encoding="utf-8")
    assert "foreman" not in text.lower(), (
        f"{template.name} still references the legacy name 'Foreman'"
    )


def test_esr_header_is_agentshore() -> None:
    esr = (_TEMPLATE_ROOT / "end_session_report.html.j2").read_text(encoding="utf-8")
    assert "AgentShore" in esr
    assert "<h1>AgentShore" in esr
