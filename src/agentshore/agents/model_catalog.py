"""Model catalog: hardcoded known models + best-effort live refresh."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping

import structlog

_logger = structlog.get_logger(__name__)

type ModelExtractor = Callable[[Mapping[str, object]], list[str]]

# Curated baseline — known models shipped with each release.
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
        "claude-opus-4-5",
        "claude-opus-4-7",
        "claude-opus-4-8",
    ],
    "codex": [
        # ChatGPT-account-compatible line (also works with API-key auth).
        # gpt-5.5 first: OpenAI's recommended default workhorse.
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.2",
        # API-key-only coding model. Kept selectable for users on API-key
        # auth, but it is NOT a default tier — ChatGPT-account sign-in rejects
        # it with HTTP 400 ("not supported when using Codex with a ChatGPT
        # account").
        "gpt-5.3-codex",
    ],
    "gemini": [
        # Gemini CLI aliases. Keep these first so the setup wizard can use
        # CLI-maintained routing when a user prefers aliases over pinned IDs.
        "auto",
        "pro",
        "flash",
        "flash-lite",
        # Current Gemini API text-generation IDs, newest first. Do not list
        # deprecated IDs here: the Gemini 3 Pro Preview and Gemini 3.1
        # Flash-Lite Preview endpoints have been shut down.
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3.1-pro-preview",
        "gemini-3.1-pro-preview-customtools",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ],
    "grok": [
        "grok-build",
        "grok-build-0.1",
        "grok-code-fast-1",
        "grok-code-fast",
        "grok-code-fast-1-0825",
    ],
}

_ANTHROPIC_PREFIXES = ("claude-",)
_OPENAI_PREFIXES = ("gpt-",)
_GEMINI_PREFIXES = ("gemini-",)
_XAI_PREFIXES = ("grok-",)


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


def _extract_gemini_model_ids(body: Mapping[str, object]) -> list[str]:
    models = body.get("models", [])
    if not isinstance(models, list):
        return []
    result: list[str] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        name = model.get("name")
        if not isinstance(name, str):
            continue
        model_id = name.removeprefix("models/")
        if not model_id.startswith(_GEMINI_PREFIXES):
            continue
        methods = model.get("supportedGenerationMethods")
        if isinstance(methods, list) and "generateContent" not in methods:
            continue
        result.append(model_id)
    return result


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


async def _fetch_gemini_models(*, timeout: float = 5.0) -> list[str]:
    """GET /v1beta/models from Gemini API. Logs and returns [] on any failure."""
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return []
    return await _fetch_live_models(
        "https://generativelanguage.googleapis.com/v1beta/models",
        provider="gemini",
        timeout=timeout,
        params={"key": api_key},
        extract=_extract_gemini_model_ids,
    )


async def _fetch_xai_models(*, timeout: float = 5.0) -> list[str]:
    """GET /v1/models from xAI. Logs and returns [] on any failure."""
    api_key = os.environ.get("XAI_API_KEY") or os.environ.get("GROK_CODE_XAI_API_KEY")
    if not api_key:
        return []
    return await _fetch_live_models(
        "https://api.x.ai/v1/models",
        provider="xai",
        timeout=timeout,
        headers={"Authorization": f"Bearer {api_key}"},
        extract=lambda body: _extract_data_model_ids(body, prefixes=_XAI_PREFIXES),
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
    elif agent_key == "gemini":
        live = await _fetch_gemini_models(timeout=timeout)
    elif agent_key == "grok":
        live = await _fetch_xai_models(timeout=timeout)

    return known + [m for m in live if m not in known_set]


def models_for_agent(agent_key: str, *, timeout: float = 5.0) -> list[str]:
    """Sync wrapper for CLI code; avoid blocking when called from an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(models_for_agent_async(agent_key, timeout=timeout))

    _logger.info("model_catalog.sync_call_inside_event_loop", agent_key=agent_key)
    return list(KNOWN_MODELS.get(agent_key, []))
