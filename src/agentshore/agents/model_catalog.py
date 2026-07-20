"""Model catalog: YAML-backed known models + best-effort live refresh.

The curated baseline ships in the wheel at ``agentshore/data/models.yaml`` and
is overridable by a single global file (``paths.GLOBAL_MODELS_PATH``) whose
per-agent-key lists wholesale-replace the bundled default when present — the
same override shape as :mod:`agentshore.agents.pricing`. :data:`KNOWN_MODELS`
is the bundled-only baseline (read once at import, deterministic for tests and
bare construction); :func:`load_model_catalog` re-reads bundled + global on
every call so a future refresh mechanism (writing the global file) is picked
up without a restart.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import os
from collections.abc import Callable, Mapping
from typing import Any

import structlog
import yaml

from agentshore.errors import ConfigError
from agentshore.paths import GLOBAL_MODELS_PATH

_logger = structlog.get_logger(__name__)

type ModelExtractor = Callable[[Mapping[str, object]], list[str]]


def _read_bundled_catalog() -> dict[str, list[str]]:
    ref = importlib.resources.files("agentshore.data").joinpath("models.yaml")
    raw = yaml.safe_load(ref.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("bundled models.yaml is malformed (root is not a mapping)")
    catalog = raw.get("models")
    if not isinstance(catalog, dict):
        raise ConfigError("bundled models.yaml must define a `models:` mapping")
    return {str(key): [str(model) for model in value] for key, value in catalog.items()}


# Curated baseline - known models shipped with each release, read once from
# the bundled YAML. Keys match AgentType values used as agent_key throughout
# the wizard. Deliberately does NOT reflect a global override (mirrors
# pricing.bundled_pricebook()) — use load_model_catalog() for override-aware
# lookups.
KNOWN_MODELS: dict[str, list[str]] = _read_bundled_catalog()

_ANTHROPIC_PREFIXES = ("claude-",)
_OPENAI_PREFIXES = ("gpt-",)


def load_model_catalog() -> dict[str, list[str]]:
    """Bundled catalog + global override (GLOBAL_MODELS_PATH), per-key replace.

    Reads the global override file fresh on every call — cheap local I/O —
    so a refresh that rewrites the file is visible without a restart. An
    override entry replaces its agent key's list wholesale; keys the override
    omits keep the bundled default.
    """
    catalog = dict(KNOWN_MODELS)
    if not GLOBAL_MODELS_PATH.exists():
        return catalog
    try:
        text = GLOBAL_MODELS_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read models file {GLOBAL_MODELS_PATH}: {exc}") from exc
    try:
        overlay_raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {GLOBAL_MODELS_PATH}: {exc}") from exc
    if overlay_raw is None:
        return catalog
    if not isinstance(overlay_raw, dict):
        raise ConfigError(
            f"models file {GLOBAL_MODELS_PATH} root must be a mapping, "
            f"got {type(overlay_raw).__name__}"
        )
    overlay = overlay_raw.get("models", overlay_raw)
    if not isinstance(overlay, dict):
        raise ConfigError(f"models file {GLOBAL_MODELS_PATH} `models:` must be a mapping")
    for key, value in overlay.items():
        if not isinstance(value, list):
            raise ConfigError(f"models file {GLOBAL_MODELS_PATH} entry `{key}` must be a list")
        catalog[str(key)] = [str(model) for model in value]
    return catalog


def write_model_catalog_override(updates: Mapping[str, list[str]]) -> None:
    """Merge *updates* into the global override file, replacing only the
    given agent keys' lists wholesale.

    Keys already present in the override but not in *updates* are preserved
    untouched — a refresh that only succeeds for some harnesses this round
    must not wipe out a previously-good override for the others. Used by the
    ``agentshore models refresh`` CLI command and the ``agents.refresh_models``
    RPC method; both write through this single function.
    """
    existing: dict[str, Any] = {}
    if GLOBAL_MODELS_PATH.exists():
        try:
            text = GLOBAL_MODELS_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"could not read models file {GLOBAL_MODELS_PATH}: {exc}") from exc
        try:
            raw: Any = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {GLOBAL_MODELS_PATH}: {exc}") from exc
        if isinstance(raw, dict):
            existing = raw

    models_block = dict(existing.get("models") or {})
    for key, value in updates.items():
        models_block[str(key)] = [str(model) for model in value]

    out: dict[str, Any] = {"models": models_block}
    GLOBAL_MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_MODELS_PATH.write_text(yaml.safe_dump(out, sort_keys=False), encoding="utf-8")


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
    known = list(load_model_catalog().get(agent_key, []))
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
    return list(load_model_catalog().get(agent_key, []))
