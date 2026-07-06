"""Model catalog: hardcoded known models + best-effort live refresh."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping

import structlog

_logger = structlog.get_logger(__name__)

type ModelExtractor = Callable[[Mapping[str, object]], list[str]]

# Curated baseline - known models shipped with each release.
# Keys match AgentType values used as agent_key throughout the wizard.
KNOWN_MODELS: dict[str, list[str]] = {
    "claude_code": [
        # CLI aliases (resolved by the claude binary itself)
        "haiku",
        "sonnet",
        "opus",
        # Pinned model IDs
        "claude-haiku-4-5",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
        "claude-opus-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        # claude-fable-5 is access-gated (Project Glasswing); selectable here but
        # yields INVALID_MODEL for users without access.
        "claude-fable-5",
    ],
    "codex": [
        # ChatGPT-account-compatible line (also works with API-key auth).
        # gpt-5.5 first: OpenAI's recommended default workhorse.
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
    ],
    "grok": [
        "grok-build",
    ],
    "antigravity": [
        # agy exposes models by display name with reasoning effort baked in (no
        # separate effort flag). Mirrors ``agy models`` (validated agy 1.0.14);
        # no live-fetch branch — kept in sync by hand.
        "Gemini 3.5 Flash (Low)",
        "Gemini 3.5 Flash (Medium)",
        "Gemini 3.5 Flash (High)",
        "Gemini 3.1 Pro (Low)",
        "Gemini 3.1 Pro (High)",
        "Claude Sonnet 4.6 (Thinking)",
        "Claude Opus 4.6 (Thinking)",
        "GPT-OSS 120B (Medium)",
    ],
}

_ANTHROPIC_PREFIXES = ("claude-",)
_OPENAI_PREFIXES = ("gpt-",)


async def _fetch_live_models(
    url: str,
    *,
    provider: str,
    timeout: float,
    extract: ModelExtractor,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> list[str]:
    """Fetch live model IDs, logging and falling back to [] on any failure."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            body = resp.json()
        if not isinstance(body, dict):
            return []
        return extract(body)
    except Exception as exc:
        _logger.debug("model_catalog.fetch_failed", provider=provider, error=str(exc))
        return []


def _extract_data_model_ids(body: Mapping[str, object], *, prefixes: tuple[str, ...]) -> list[str]:
    data = body.get("data", [])
    if not isinstance(data, list):
        return []
    return [
        model_id
        for model in data
        if isinstance(model, dict)
        and isinstance(model_id := model.get("id"), str)
        and model_id.startswith(prefixes)
    ]


async def _fetch_anthropic_models(*, timeout: float = 5.0) -> list[str]:
    """GET /v1/models from Anthropic. Logs and returns [] on any failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []
    return await _fetch_live_models(
        "https://api.anthropic.com/v1/models",
        provider="anthropic",
        timeout=timeout,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        extract=lambda body: _extract_data_model_ids(body, prefixes=_ANTHROPIC_PREFIXES),
    )


async def _fetch_openai_models(*, timeout: float = 5.0) -> list[str]:
    """GET /v1/models from OpenAI. Logs and returns [] on any failure."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return []
    return await _fetch_live_models(
        "https://api.openai.com/v1/models",
        provider="openai",
        timeout=timeout,
        headers={"Authorization": f"Bearer {api_key}"},
        extract=lambda body: _extract_data_model_ids(body, prefixes=_OPENAI_PREFIXES),
    )


async def models_for_agent_async(agent_key: str, *, timeout: float = 5.0) -> list[str]:
    """Return deduplicated model list: known first, then live extras.

    Known models always appear in catalog order. Live API results contribute
    only entries not already present, appended at the end.
    """
    known = list(KNOWN_MODELS.get(agent_key, []))
    known_set = set(known)

    live: list[str] = []
    if agent_key == "claude_code":
        live = await _fetch_anthropic_models(timeout=timeout)
    elif agent_key == "codex":
        live = await _fetch_openai_models(timeout=timeout)
    return known + [m for m in live if m not in known_set]


def models_for_agent(agent_key: str, *, timeout: float = 5.0) -> list[str]:
    """Sync wrapper for CLI code; avoid blocking when called from an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(models_for_agent_async(agent_key, timeout=timeout))

    _logger.info("model_catalog.sync_call_inside_event_loop", agent_key=agent_key)
    return list(KNOWN_MODELS.get(agent_key, []))
