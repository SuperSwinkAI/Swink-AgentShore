"""Claude Code model discovery via a live, paid LLM agent dispatch.

Claude Code (unlike Codex/Grok/Antigravity — see ``model_discovery.py``) has
no CLI-native mechanism for listing its current models: no flag, no
subcommand, and no separate bundled manifest — its model IDs are baked into
the compiled binary with no way to distinguish current from years-deprecated
ones (confirmed via ``strings`` on the installed binary; see the model-catalog
spike notes in docs/design/agents/DESIGN.md). The only way to get a live
answer is to ask a running Claude Code agent to look it up.

This is a categorically different mechanism from the free CLI probes in
``model_discovery.py``: it spends real API tokens, so every call site in this
codebase must treat it as opt-in, never automatic. Measured cost (Haiku tier,
--safe-mode, WebSearch+WebFetch, real runs during development): a *successful*
run costs roughly $0.30-0.50 across ~10-13 turns of web search; a capped run
that exhausts its budget before finishing costs whatever the cap is and
produces no usable answer. There is no reliable way to make this cheap — the
cost floor is the built-in tool-schema system prompt (already ~$0.01 per call
before any search) plus however many searches the model needs to find a
confident, current answer. Budget the DEFAULT_MAX_BUDGET_USD ceiling generously
enough to let a normal run finish rather than truncating it into a wasted
partial spend.

Flags used and why:

    --safe-mode                 Disables CLAUDE.md/plugins/hooks/MCP/custom
                                 commands so the probe never picks up the
                                 caller's project config or fires unrelated
                                 hooks (observed: a bare non-safe-mode call
                                 from this very repo picked up its CLAUDE.md
                                 and ran SessionEnd hooks). Auth (OAuth or
                                 API key) and model selection are unaffected.
    (NOT --bare)                --bare forces ANTHROPIC_API_KEY-only auth,
                                 disabling OAuth/keychain — that would defeat
                                 the entire point of asking the CLI instead of
                                 hitting the raw provider API directly.
    --allowedTools WebSearch,WebFetch
                                 Both, not just WebSearch: an early test run
                                 that allowed only WebSearch got its WebFetch
                                 calls denied and compensated with 10 separate
                                 searches over 13 turns ($0.37) instead of
                                 fetching the canonical docs page directly.
    --max-budget-usd <cap>      A real, deterministic hard spend ceiling —
                                 confirmed empirically: the CLI exits cleanly
                                 with subtype "error_max_budget_usd" and
                                 is_error=true rather than hanging or silently
                                 overspending.
    --output-format json        Single JSON envelope with both the final
                                 text (``result``) and authoritative billed
                                 cost (``total_cost_usd``) — used directly
                                 instead of re-deriving cost via pricing.yaml.

Blocking in nature, matching agents.model_discovery / agents.auth_probe;
async callers should wrap calls in asyncio.to_thread.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from typing import Literal

import structlog

from agentshore.agents.model_discovery import _run_probe

_logger = structlog.get_logger(__name__)

LlmDiscoveryStatus = Literal["ok", "unavailable", "timeout", "budget_exceeded", "error"]

# A successful run has taken up to ~125s across ~13 turns of search in
# practice; give real headroom above that before treating it as hung.
DEFAULT_DISCOVERY_TIMEOUT_S = 240.0

# Comfortably above the observed successful-run cost (~$0.30-0.50) so a
# legitimate run isn't truncated into a wasted partial spend, while still
# bounding worst-case runaway cost well under $1.
DEFAULT_MAX_BUDGET_USD = 0.75

DEFAULT_MODEL_TIER = "haiku"

_PROMPT = (
    "Use web search and web fetch to confirm, from Anthropic's current "
    "official documentation, the model IDs and generation aliases (e.g. "
    "sonnet/opus/haiku/fable) currently selectable via the Claude Code "
    "CLI's --model flag. Respond with ONLY a JSON array of strings - "
    "nothing else, no prose, no markdown fences."
)


@dataclass(frozen=True)
class LlmDiscoveryResult:
    """Outcome of asking a live Claude Code agent for its current model list.

    ``status`` extends the free-probe vocabulary with ``budget_exceeded``
    (the run hit ``max_budget_usd`` before producing an answer — distinct
    from ``error`` because money was spent with nothing usable to show for
    it, which callers should surface differently). ``cost_usd`` is the
    provider-reported billed cost (``total_cost_usd``) whenever the CLI
    produced a JSON envelope at all, including failed/capped runs.
    """

    agent_key: Literal["claude_code"]
    models: tuple[str, ...]
    status: LlmDiscoveryStatus
    detail: str = ""
    cost_usd: float = 0.0
    tier: str = DEFAULT_MODEL_TIER


def _strip_markdown_fences(text: str) -> str:
    """Strip a leading/trailing ``` or ```json fence, if present.

    Observed necessary in practice: a Haiku run explicitly told "no markdown
    fences" still wrapped its answer in one.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_model_list(result_text: str) -> tuple[str, ...] | None:
    """Parse the agent's final text as a JSON array of non-empty strings."""
    try:
        payload = json.loads(_strip_markdown_fences(result_text))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload:
        return None
    if not all(isinstance(item, str) and item.strip() for item in payload):
        return None
    return tuple(payload)


def _classify_envelope(envelope: dict[str, object]) -> LlmDiscoveryResult | None:
    """Map a parsed --output-format json envelope to a terminal result.

    Returns None when the envelope represents a successful, parseable
    response the caller should extract models from.
    """
    cost = envelope.get("total_cost_usd")
    cost_usd = float(cost) if isinstance(cost, (int, float)) else 0.0
    subtype = envelope.get("subtype")
    is_error = bool(envelope.get("is_error"))

    if subtype == "error_max_budget_usd":
        errors = envelope.get("errors")
        detail = (
            ", ".join(str(e) for e in errors) if isinstance(errors, list) else "budget exceeded"
        )
        return LlmDiscoveryResult("claude_code", (), "budget_exceeded", detail, cost_usd)
    if is_error:
        errors = envelope.get("errors")
        detail = (
            ", ".join(str(e) for e in errors)
            if isinstance(errors, list)
            else str(subtype or "error")
        )
        return LlmDiscoveryResult("claude_code", (), "error", detail, cost_usd)
    return None


def discover_claude_code_models_via_agent(
    *,
    binary: str = "claude",
    tier: str = DEFAULT_MODEL_TIER,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    timeout: float = DEFAULT_DISCOVERY_TIMEOUT_S,
) -> LlmDiscoveryResult:
    """Ask a live Claude Code agent for its current model list.

    Spends real API tokens (see module docstring for measured cost). Callers
    MUST treat this as an explicit, user-opted-in action — never call it from
    an automatic/scheduled path. Runs in an isolated scratch cwd (in addition
    to --safe-mode) so it can never pick up a caller's project config.
    """
    resolved = shutil.which(binary)
    if resolved is None:
        return LlmDiscoveryResult(
            "claude_code", (), "unavailable", f"{binary!r} not found on PATH", tier=tier
        )

    argv = [
        resolved,
        "-p",
        _PROMPT,
        "--model",
        tier,
        "--output-format",
        "json",
        "--safe-mode",
        "--allowedTools",
        "WebSearch,WebFetch",
        "--max-budget-usd",
        f"{max_budget_usd:g}",
    ]

    with tempfile.TemporaryDirectory(prefix="agentshore-model-discovery-") as scratch_cwd:
        result = _run_probe(argv, timeout=timeout, cwd=scratch_cwd)

    if result.timed_out:
        return LlmDiscoveryResult(
            "claude_code", (), "timeout", f"timed out after {timeout:g}s", tier=tier
        )
    if result.spawn_error:
        return LlmDiscoveryResult("claude_code", (), "error", result.spawn_error, tier=tier)

    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        detail = f"unparseable --output-format json envelope: {result.stdout.strip()[:200]!r}"
        return LlmDiscoveryResult("claude_code", (), "error", detail, tier=tier)
    if not isinstance(envelope, dict):
        return LlmDiscoveryResult(
            "claude_code", (), "error", "envelope root is not an object", tier=tier
        )

    terminal = _classify_envelope(envelope)
    if terminal is not None:
        _logger.info(
            "model_discovery_llm.dispatch_failed",
            status=terminal.status,
            cost_usd=terminal.cost_usd,
            tier=tier,
        )
        return LlmDiscoveryResult(
            terminal.agent_key,
            terminal.models,
            terminal.status,
            terminal.detail,
            terminal.cost_usd,
            tier,
        )

    cost_raw = envelope.get("total_cost_usd")
    cost_usd = float(cost_raw) if isinstance(cost_raw, (int, float)) else 0.0
    result_text = envelope.get("result")
    if not isinstance(result_text, str):
        return LlmDiscoveryResult(
            "claude_code", (), "error", "envelope missing string `result`", cost_usd, tier
        )

    models = _parse_model_list(result_text)
    if not models:
        detail = f"agent response was not a JSON array of model strings: {result_text[:200]!r}"
        return LlmDiscoveryResult("claude_code", (), "error", detail, cost_usd, tier)

    _logger.info(
        "model_discovery_llm.dispatch_succeeded",
        model_count=len(models),
        cost_usd=cost_usd,
        tier=tier,
    )
    return LlmDiscoveryResult(
        "claude_code", models, "ok", f"cost_usd={cost_usd:.4f}", cost_usd, tier
    )
