"""Live model resolution (model_resolver) and the CLI-error classification
that feeds it (structured JSONL error events, 2026-10 CLI audit)."""

from __future__ import annotations

import json

import pytest

from agentshore.agents import model_discovery, model_resolver
from agentshore.agents.cli.errors import _classify_error
from agentshore.agents.model_discovery import DiscoveryResult
from agentshore.agents.model_tiers import effective_model_tier_config
from agentshore.config.models import AgentConfig, ModelTierConfig
from agentshore.errors import ErrorClass
from agentshore.state import AgentType

_AGY_LIVE = (
    "Gemini 3.8 Flash (High)",
    "Gemini 3.7 Flash (High)",
    "Gemini 3.1 Pro (High)",
    "GPT-OSS 120B (Medium)",
)


def _record(agent_type: AgentType, models: tuple[str, ...], **kw: object) -> None:
    model_resolver.record_discovery(
        agent_type,
        DiscoveryResult(agent_type.value, models, "ok", **kw),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


def test_no_discovery_passes_preference_through() -> None:
    assert model_resolver.resolve_model(AgentType.GROK, "grok-4.5") == "grok-4.5"


def test_available_preference_is_kept() -> None:
    _record(AgentType.ANTIGRAVITY, _AGY_LIVE)
    assert (
        model_resolver.resolve_model(AgentType.ANTIGRAVITY, "Gemini 3.7 Flash (High)")
        == "Gemini 3.7 Flash (High)"
    )


def test_retired_model_moves_to_newest_same_family() -> None:
    _record(AgentType.ANTIGRAVITY, _AGY_LIVE)
    assert (
        model_resolver.resolve_model(AgentType.ANTIGRAVITY, "Gemini 3.5 Flash (High)")
        == "Gemini 3.8 Flash (High)"
    )


def test_unknown_model_falls_back_to_tier_default_then_cli_default() -> None:
    _record(AgentType.GROK, ("grok-4.7", "grok-4.5"), default="grok-4.7")
    assert model_resolver.resolve_model(AgentType.GROK, "custom", "grok-4.5") == "grok-4.5"
    assert model_resolver.resolve_model(AgentType.GROK, "custom", "gone") == "grok-4.7"


def test_invalid_model_is_excluded_even_without_a_live_list() -> None:
    model_resolver.mark_model_invalid(AgentType.CLAUDE_CODE, "claude-opus-4-8")
    assert model_resolver.resolve_model(AgentType.CLAUDE_CODE, "claude-opus-4-8", "opus") == "opus"


def test_invalid_model_is_excluded_from_live_list() -> None:
    _record(AgentType.CODEX, ("gpt-5.6-terra", "gpt-5.4"))
    model_resolver.mark_model_invalid(AgentType.CODEX, "gpt-5.4")
    assert model_resolver.resolve_model(AgentType.CODEX, "gpt-5.4", None) == "gpt-5.6-terra"


# ---------------------------------------------------------------------------
# resolve_effort
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("effort", "expected"),
    [("xhigh", "high"), ("max", "high"), ("low", "low")],
)
def test_effort_clamped_to_static_vocabulary(effort: str, expected: str) -> None:
    vocab = ("low", "medium", "high")
    assert model_resolver.resolve_effort(AgentType.GROK, "grok-4.5", effort, vocab) == expected


def test_effort_uses_live_per_model_levels() -> None:
    _record(
        AgentType.CODEX,
        ("gpt-5.6-luna",),
        efforts={"gpt-5.6-luna": ("low", "medium", "high", "xhigh", "max")},
    )
    assert (
        model_resolver.resolve_effort(AgentType.CODEX, "gpt-5.6-luna", "minimal", ("minimal",))
        == "low"
    )


# ---------------------------------------------------------------------------
# effective_model_tier_config integration
# ---------------------------------------------------------------------------


def test_effective_config_resolves_stale_configured_model_and_effort() -> None:
    _record(AgentType.ANTIGRAVITY, _AGY_LIVE)
    cfg = AgentConfig(
        model_tiers={"medium": ModelTierConfig(model="Gemini 3.5 Flash (High)", max=3)}
    )
    resolved = effective_model_tier_config(AgentType.ANTIGRAVITY, cfg, "medium")
    assert resolved.model == "Gemini 3.8 Flash (High)"
    assert resolved.max == 3

    grok = AgentConfig(
        model_tiers={"large": ModelTierConfig(model="grok-4.5", reasoning_effort="max")}
    )
    assert effective_model_tier_config(AgentType.GROK, grok, "large").reasoning_effort == "high"


def test_swink_coding_is_never_substituted() -> None:
    _record(AgentType.SWINK_CODING, ("small", "medium", "large"))
    cfg = AgentConfig(model_tiers={"small": ModelTierConfig(model="ollama:qwen@http://x:1")})
    assert (
        effective_model_tier_config(AgentType.SWINK_CODING, cfg, "small").model
        == "ollama:qwen@http://x:1"
    )


# ---------------------------------------------------------------------------
# ensure_discovered
# ---------------------------------------------------------------------------


async def test_ensure_discovered_probes_once_and_reprobes_after_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    written: list[dict[str, list[str]]] = []

    def probe(**_kw: object) -> DiscoveryResult:
        calls.append("probe")
        return DiscoveryResult("antigravity", _AGY_LIVE, "ok")

    monkeypatch.setattr(model_discovery, "free_discovery_func", lambda _key: probe)
    monkeypatch.setattr(
        "agentshore.agents.model_catalog.write_model_catalog_override", written.append
    )

    await model_resolver.ensure_discovered(AgentType.ANTIGRAVITY)
    await model_resolver.ensure_discovered(AgentType.ANTIGRAVITY)
    assert calls == ["probe"]
    assert written == [{"antigravity": list(_AGY_LIVE)}]

    model_resolver.mark_model_invalid(AgentType.ANTIGRAVITY, "Gemini 3.8 Flash (High)")
    await model_resolver.ensure_discovered(AgentType.ANTIGRAVITY)
    assert calls == ["probe", "probe"]
    assert (
        model_resolver.resolve_model(AgentType.ANTIGRAVITY, "Gemini 3.8 Flash (High)")
        == "Gemini 3.7 Flash (High)"
    )


async def test_ensure_discovered_failure_leaves_preferences_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_discovery,
        "free_discovery_func",
        lambda _key: lambda **_kw: DiscoveryResult("grok", (), "timeout"),
    )
    await model_resolver.ensure_discovered(AgentType.GROK)
    assert model_resolver.resolve_model(AgentType.GROK, "grok-4.5") == "grok-4.5"


# ---------------------------------------------------------------------------
# CLI error classification (structured JSONL error events)
# ---------------------------------------------------------------------------

_CLAUDE_RATE_LIMIT_INFO = json.dumps(
    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}
)


def _claude_failed(code: str, text: str) -> str:
    assistant = {"type": "assistant", "error": code, "message": {"content": [{"text": text}]}}
    result = {"type": "result", "is_error": True, "result": text, "pad": "x" * 1200}
    return "\n".join(json.dumps(e) for e in (assistant, result))


def test_claude_informational_rate_limit_event_is_not_a_rate_limit() -> None:
    # A run killed after claude's per-run rate_limit_event must not read as quota.
    stdout = '{"type":"system","subtype":"init"}\n' + _CLAUDE_RATE_LIMIT_INFO + "\n"
    assert _classify_error(-9, "", stdout) != ErrorClass.RATE_LIMIT


@pytest.mark.parametrize(
    ("code", "text", "expected"),
    [
        ("model_not_found", "There's an issue with the selected model", ErrorClass.INVALID_MODEL),
        ("authentication_failed", "Not logged in · Please run /login", ErrorClass.AUTH),
        ("rate_limit", "You've hit your weekly limit", ErrorClass.RATE_LIMIT),
    ],
)
def test_claude_structured_error_code_wins(code: str, text: str, expected: ErrorClass) -> None:
    assert _classify_error(1, "", _claude_failed(code, text)) == expected


def test_codex_turn_failed_model_rejection_is_invalid_model() -> None:
    msg = (
        '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"The \'gpt-5.4\' model is not supported when using Codex with a '
        'ChatGPT account."}}'
    )
    stdout = json.dumps({"type": "turn.failed", "error": {"message": msg}})
    assert _classify_error(1, "", stdout) == ErrorClass.INVALID_MODEL


def test_codex_generic_bad_request_does_not_mark_model_invalid() -> None:
    msg = (
        '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"Unsupported value: \'minimal\' is not supported"}}'
    )
    stdout = json.dumps({"type": "turn.failed", "error": {"message": msg}})
    assert _classify_error(1, "", stdout) != ErrorClass.INVALID_MODEL


def test_agy_retired_model_is_invalid_model() -> None:
    stderr = (
        'error: invalid model selection (--model "Gemini 3.5 Flash (High)" --effort ""): '
        "model Gemini 3.5 Flash (High) is not recognized as a known model"
    )
    assert _classify_error(1, stderr, "") == ErrorClass.INVALID_MODEL


def test_grok_not_signed_in_is_auth() -> None:
    stdout = json.dumps({"type": "error", "message": "Not signed in. To authenticate..."})
    assert _classify_error(1, "", stdout) == ErrorClass.AUTH


def test_swink_unknown_tier_is_invalid_model() -> None:
    stderr = 'unknown tier "bogus"; expected small|medium|large'
    assert _classify_error(1, stderr, "") == ErrorClass.INVALID_MODEL
