"""Tests for model_catalog: known baseline, live-fetch dedup, failure fallback."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.agents.model_catalog import (
    KNOWN_MODELS,
    _fetch_anthropic_models,
    _fetch_gemini_models,
    _fetch_openai_models,
    _fetch_xai_models,
    models_for_agent,
)

# ---------------------------------------------------------------------------
# models_for_agent — baseline
# ---------------------------------------------------------------------------


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


def test_known_models_first_in_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    live = ["claude-new-model-9000", "claude-haiku-4-5"]  # haiku already known
    with patch(
        "agentshore.agents.model_catalog._fetch_anthropic_models",
        new=AsyncMock(return_value=live),
    ):
        result = models_for_agent("claude_code")

    known = KNOWN_MODELS["claude_code"]
    assert result[: len(known)] == known
    assert "claude-new-model-9000" in result
    # duplicate should not appear
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


def test_codex_known_models_exclude_legacy_o_series() -> None:
    assert "o1" not in KNOWN_MODELS["codex"]
    assert "o3" not in KNOWN_MODELS["codex"]
    assert "o4-mini" not in KNOWN_MODELS["codex"]


def test_gemini_falls_back_to_known_models_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with (
        patch("agentshore.agents.model_catalog._fetch_anthropic_models", new=AsyncMock()) as mock_a,
        patch("agentshore.agents.model_catalog._fetch_openai_models", new=AsyncMock()) as mock_o,
        patch(
            "agentshore.agents.model_catalog._fetch_gemini_models", new=AsyncMock(return_value=[])
        ) as mock_g,
    ):
        result = models_for_agent("gemini")

    mock_a.assert_not_called()
    mock_o.assert_not_called()
    mock_g.assert_called_once()
    assert result == KNOWN_MODELS["gemini"]


def test_gemini_live_extras_appended_after_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    live = ["gemini-future", "gemini-2.5-flash"]
    with patch(
        "agentshore.agents.model_catalog._fetch_gemini_models", new=AsyncMock(return_value=live)
    ):
        result = models_for_agent("gemini")

    known = KNOWN_MODELS["gemini"]
    assert result[: len(known)] == known
    assert "gemini-future" in result
    assert result.count("gemini-2.5-flash") == 1


def test_grok_known_models_include_build_aliases() -> None:
    assert KNOWN_MODELS["grok"] == [
        "grok-build",
        "grok-build-0.1",
        "grok-code-fast-1",
        "grok-code-fast",
        "grok-code-fast-1-0825",
    ]


def test_xai_fetch_used_for_grok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    live = ["grok-future"]
    with patch(
        "agentshore.agents.model_catalog._fetch_xai_models", new=AsyncMock(return_value=live)
    ):
        result = models_for_agent("grok")

    assert "grok-future" in result


# ---------------------------------------------------------------------------
# _fetch_anthropic_models — failure modes
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _fetch_openai_models — failure modes
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _fetch_gemini_models — failure modes
# ---------------------------------------------------------------------------


def test_fetch_gemini_returns_empty_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert asyncio.run(_fetch_gemini_models()) == []


def test_fetch_gemini_returns_empty_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    import httpx

    with (
        patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.RequestError("timeout"))),
        patch("agentshore.agents.model_catalog._logger") as logger,
    ):
        assert asyncio.run(_fetch_gemini_models()) == []

    logger.debug.assert_called_once_with(
        "model_catalog.fetch_failed",
        provider="gemini",
        error="timeout",
    )


def test_fetch_gemini_filters_non_generation_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "models": [
            {
                "name": "models/gemini-2.5-flash",
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/gemini-embedding-001",
                "supportedGenerationMethods": ["embedContent"],
            },
            {"name": "models/gemma-4", "supportedGenerationMethods": ["generateContent"]},
        ]
    }
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        result = asyncio.run(_fetch_gemini_models())

    assert result == ["gemini-2.5-flash"]


def test_fetch_xai_returns_empty_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_CODE_XAI_API_KEY", raising=False)
    assert asyncio.run(_fetch_xai_models()) == []


def test_fetch_xai_uses_grok_code_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("GROK_CODE_XAI_API_KEY", "xai-test")
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "data": [
            {"id": "grok-build-0.1"},
            {"id": "not-grok-model"},
        ]
    }
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)) as get:
        result = asyncio.run(_fetch_xai_models())

    assert result == ["grok-build-0.1"]
    headers = get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer xai-test"


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
