"""Tests for model_catalog: known baseline, live-fetch dedup, failure fallback."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.agents.model_catalog import (
    KNOWN_MODELS,
    _fetch_anthropic_models,
    _fetch_openai_models,
    models_for_agent,
)
from agentshore.agents.pricing import bundled_pricebook


def test_known_models_returned_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = models_for_agent("claude_code")
    assert result == KNOWN_MODELS["claude_code"]


def test_unknown_agent_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert models_for_agent("nonexistent_agent") == []


def test_models_for_agent_in_running_loop_skips_live_fetch() -> None:
    async_models = MagicMock()

    async def _call() -> list[str]:
        with patch("agentshore.agents.model_catalog.models_for_agent_async", async_models):
            return models_for_agent("codex")

    result = asyncio.run(_call())
    assert result == KNOWN_MODELS["codex"]
    async_models.assert_not_called()


def test_claude_catalog_includes_fable_and_current_sonnet() -> None:
    claude_models = KNOWN_MODELS["claude_code"]

    assert "claude-fable-5" in claude_models
    assert "claude-sonnet-5" in claude_models
    assert "claude-opus-4-6" in claude_models
    assert "claude-opus-4-8" in claude_models


def test_known_models_first_in_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    live = ["claude-new-model-9000", "claude-haiku-4-5"]
    with patch(
        "agentshore.agents.model_catalog._fetch_anthropic_models",
        new=AsyncMock(return_value=live),
    ):
        result = models_for_agent("claude_code")

    known = KNOWN_MODELS["claude_code"]
    assert result[: len(known)] == known
    assert "claude-new-model-9000" in result
    assert result.count("claude-haiku-4-5") == 1


def test_live_extras_appended_after_known() -> None:
    live = ["claude-future-1", "claude-future-2"]
    with patch(
        "agentshore.agents.model_catalog._fetch_anthropic_models",
        new=AsyncMock(return_value=live),
    ):
        result = models_for_agent("claude_code")

    known_len = len(KNOWN_MODELS["claude_code"])
    assert result[known_len:] == live


def test_openai_fetch_used_for_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    live = ["gpt-99"]
    with patch(
        "agentshore.agents.model_catalog._fetch_openai_models", new=AsyncMock(return_value=live)
    ):
        result = models_for_agent("codex")

    assert "gpt-99" in result


def test_codex_known_models_exclude_legacy_and_deprecated_models() -> None:
    assert "o1" not in KNOWN_MODELS["codex"]
    assert "o3" not in KNOWN_MODELS["codex"]
    assert "o4-mini" not in KNOWN_MODELS["codex"]
    assert "gpt-5.2" not in KNOWN_MODELS["codex"]
    assert "gpt-5.3-codex" not in KNOWN_MODELS["codex"]
    # gpt-5.5-pro 400s under ChatGPT-account auth (codex's default mode),
    # which permanently kills the agent via INVALID_MODEL; API-key users
    # still reach it through the live-fetch extras.
    assert "gpt-5.5-pro" not in KNOWN_MODELS["codex"]


def test_codex_catalog_includes_current_lineup() -> None:
    codex_models = KNOWN_MODELS["codex"]

    assert "gpt-5.5" in codex_models
    assert "gpt-5.4-nano" in codex_models


def test_grok_known_models_hard_pinned_to_build() -> None:
    # grok is hard-pinned: exactly one entry, grok-build.
    assert KNOWN_MODELS["grok"] == ["grok-build"]


def test_grok_no_live_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    # models_for_agent for grok must never call a live xAI API.
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    result = models_for_agent("grok")
    assert result == ["grok-build"]


def test_antigravity_known_models_include_non_google_backends() -> None:
    # agy (validated against `agy models`, agy 1.0.14) exposes non-Google
    # backends alongside Gemini; all three must be selectable.
    antigravity = KNOWN_MODELS["antigravity"]
    assert "Claude Sonnet 4.6 (Thinking)" in antigravity
    assert "Claude Opus 4.6 (Thinking)" in antigravity
    assert "GPT-OSS 120B (Medium)" in antigravity
    assert "Gemini 3.1 Pro (High)" in antigravity


def test_every_catalog_model_has_a_pricing_row() -> None:
    # Catalog↔pricing invariant: a selectable model with no `models:` row in
    # pricing.yaml bills at its agent_defaults rate — the wrong tier for
    # anything not sonnet-priced (opus ~5x under-billed, haiku ~3x over).
    book = bundled_pricebook()
    missing = [
        (agent_key, model)
        for agent_key, models in KNOWN_MODELS.items()
        for model in models
        if model not in book.models
    ]
    assert missing == []


def test_antigravity_no_live_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    # agy has no httpx model endpoint; the list is the hand-maintained mirror.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    result = models_for_agent("antigravity")
    assert result == KNOWN_MODELS["antigravity"]


def test_fetch_anthropic_returns_empty_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert asyncio.run(_fetch_anthropic_models()) == []


def test_fetch_anthropic_returns_empty_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    import httpx

    with (
        patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.RequestError("timeout"))),
        patch("agentshore.agents.model_catalog._logger") as logger,
    ):
        assert asyncio.run(_fetch_anthropic_models()) == []

    logger.debug.assert_called_once_with(
        "model_catalog.fetch_failed",
        provider="anthropic",
        error="timeout",
    )


def test_fetch_anthropic_filters_non_claude_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "data": [
            {"id": "claude-sonnet-4-5"},
            {"id": "not-claude-model"},
            {"id": "claude-opus-4-7"},
        ]
    }
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        result = asyncio.run(_fetch_anthropic_models())

    assert result == ["claude-sonnet-4-5", "claude-opus-4-7"]
    assert "not-claude-model" not in result


def test_fetch_openai_returns_empty_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert asyncio.run(_fetch_openai_models()) == []


def test_fetch_openai_returns_empty_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import httpx

    with (
        patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.RequestError("timeout"))),
        patch("agentshore.agents.model_catalog._logger") as logger,
    ):
        assert asyncio.run(_fetch_openai_models()) == []

    logger.debug.assert_called_once_with(
        "model_catalog.fetch_failed",
        provider="openai",
        error="timeout",
    )


def test_fetch_openai_filters_to_relevant_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "data": [
            {"id": "gpt-5.5"},
            {"id": "whisper-1"},
            {"id": "o3"},
            {"id": "dall-e-3"},
            {"id": "o4-mini"},
        ]
    }
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        result = asyncio.run(_fetch_openai_models())

    assert "gpt-5.5" in result
    assert "o3" not in result
    assert "o4-mini" not in result
    assert "whisper-1" not in result
    assert "dall-e-3" not in result
