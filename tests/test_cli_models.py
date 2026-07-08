"""Tests for the ``agentshore models refresh`` CLI subcommand."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from agentshore.agents.model_refresh import HarnessRefreshOutcome, ModelRefreshSummary
from agentshore.cli import main


def _summary(
    *,
    dry_run: bool = False,
    unpriced_models: tuple[tuple[str, str], ...] = (),
    **harness_overrides: HarnessRefreshOutcome,
) -> ModelRefreshSummary:
    base = {
        "codex": HarnessRefreshOutcome("codex", "ok", ("gpt-5.5",)),
        "grok": HarnessRefreshOutcome("grok", "ok", ("grok-4.5",)),
        "antigravity": HarnessRefreshOutcome("antigravity", "ok", ("model-x",)),
        "claude_code": HarnessRefreshOutcome("claude_code", "skipped", (), detail="not requested"),
    }
    base.update(harness_overrides)
    return ModelRefreshSummary(harnesses=base, unpriced_models=unpriced_models, dry_run=dry_run)


def test_refresh_dry_run_shows_diff_and_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_refresh(**kwargs: object) -> ModelRefreshSummary:
        captured.update(kwargs)
        return _summary(
            dry_run=True,
            codex=HarnessRefreshOutcome("codex", "ok", ("gpt-5.5", "gpt-5.6"), added=("gpt-5.6",)),
        )

    monkeypatch.setattr("agentshore.agents.model_refresh.refresh_model_catalog", fake_refresh)

    result = CliRunner().invoke(main, ["models", "refresh", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert captured["dry_run"] is True
    assert captured["include_claude_code"] is False
    assert "codex: +gpt-5.6" in result.output
    assert "dry run" in result.output.lower()


def test_refresh_no_changes_reports_no_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentshore.agents.model_refresh.refresh_model_catalog", lambda **_kw: _summary()
    )

    result = CliRunner().invoke(main, ["models", "refresh"])

    assert result.exit_code == 0, result.output
    assert "No changes." in result.output


def test_refresh_reports_unpriced_models(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = _summary(
        unpriced_models=(("codex", "new-model"),),
        codex=HarnessRefreshOutcome("codex", "ok", ("new-model",), added=("new-model",)),
    )
    monkeypatch.setattr(
        "agentshore.agents.model_refresh.refresh_model_catalog", lambda **_kw: summary
    )

    result = CliRunner().invoke(main, ["models", "refresh"])

    assert result.exit_code == 0, result.output
    assert "no pricing.yaml row" in result.output
    assert "codex: new-model" in result.output


def test_refresh_include_claude_code_declined_skips_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_refresh(**kwargs: object) -> ModelRefreshSummary:
        captured.update(kwargs)
        return _summary()

    monkeypatch.setattr("agentshore.agents.model_refresh.refresh_model_catalog", fake_refresh)

    result = CliRunner().invoke(main, ["models", "refresh", "--include-claude-code"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Skipping Claude Code" in result.output
    assert captured["include_claude_code"] is False


def test_refresh_include_claude_code_yes_flag_skips_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_refresh(**kwargs: object) -> ModelRefreshSummary:
        captured.update(kwargs)
        return _summary()

    monkeypatch.setattr("agentshore.agents.model_refresh.refresh_model_catalog", fake_refresh)

    result = CliRunner().invoke(main, ["models", "refresh", "--include-claude-code", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Continue?" not in result.output
    assert captured["include_claude_code"] is True


def test_refresh_writes_message_when_changes_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = _summary(
        grok=HarnessRefreshOutcome("grok", "ok", ("grok-4.5", "grok-new"), added=("grok-new",))
    )
    monkeypatch.setattr(
        "agentshore.agents.model_refresh.refresh_model_catalog", lambda **_kw: summary
    )

    result = CliRunner().invoke(main, ["models", "refresh"])

    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output
