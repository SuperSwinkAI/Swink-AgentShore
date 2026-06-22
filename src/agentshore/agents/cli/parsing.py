"""Per-agent JSONL/stream output parsing for the CLI agent adapter.

Extracted from ``cli_agent``: all ``_extract_*``, ``_parse_*``, the
``CliOutputFormat`` protocol, ``_FunctionFormat`` adapter, the ``_PARSERS``
registry, ``_is_terminal_event``, and ``_ReadOutput``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Protocol

from agentshore.agents._jsonl import (
    _iter_json_events,
    _max_usage,
    _usage_totals_from_dict,
    _UsageTotals,
)
from agentshore.state import AgentType

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# _ReadOutput
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ReadOutput:
    raw: str
    usage: _UsageTotals
    session_id: str | None


# ---------------------------------------------------------------------------
# Terminal-event detection
# ---------------------------------------------------------------------------

# The terminal stream event each CLI emits once its response is fully written.
# Detecting it lets the idle watcher apply the short _POST_RESPONSE_GRACE_S
# (60s) instead of waiting the full stream_idle_timeout (default 1800s) for a
# finished-but-unexited subprocess. Previously only Claude was wired up, so a
# finished codex lingered up to 30 min — stacking memory across
# plays toward OOM (#21). Codex emits ``turn.completed``; Claude emits ``type: "result"``.
_TERMINAL_EVENT_TYPES: Final[dict[AgentType, frozenset[str]]] = {
    AgentType.CLAUDE_CODE: frozenset({"result"}),
    AgentType.CODEX: frozenset({"turn.completed"}),
    AgentType.GROK: frozenset({"end"}),
}


def _is_terminal_event(line: bytes, agent_type: AgentType) -> bool:
    """Return True if *line* is *agent_type*'s response-complete stream event.

    This is the final event the CLI emits after completing a response.
    Detecting it lets the idle watcher switch to a short grace period so
    lingering background tasks don't block process exit for 30 minutes (#21).
    """
    terminal_types = _TERMINAL_EVENT_TYPES.get(agent_type)
    if not terminal_types:
        return False
    # Cheap pre-filter: skip json.loads unless a terminal type name appears.
    if not any(t.encode() in line for t in terminal_types):
        return False
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(event, dict):
        return False
    # Grok CLI uses ``event`` (not ``type``) as the event-type key in some output shapes.
    return event.get("type") in terminal_types or (
        agent_type == AgentType.GROK and event.get("event") in terminal_types
    )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _extract_session_id_from_jsonl(raw: str) -> str | None:
    for event in _iter_json_events(raw):
        for key in ("session_id", "thread_id"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _maybe_parse_usage(line: bytes, current: _UsageTotals) -> _UsageTotals:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return current

    usage: object = {}
    if event.get("type") == "result":
        usage = event.get("usage", {})
    elif event.get("type") == "assistant":
        message = event.get("message", {})
        usage = message.get("usage", {}) if isinstance(message, dict) else {}
    elif event.get("type") == "message_delta":
        usage = event.get("usage", {})

    if not isinstance(usage, dict):
        return current
    parsed = _usage_totals_from_dict(usage, input_includes_cache=False)
    # Claude Code stamps an authoritative ``total_cost_usd`` on the terminal
    # ``result`` event. Prefer it over token-derivation: it bills the exact model
    # and the 5m/1h ephemeral-cache tiers the static pricing table can't see, and
    # was observed ~2x higher than the token-derived figure (dashboard undercount).
    if event.get("type") == "result":
        reported = event.get("total_cost_usd")
        if isinstance(reported, int | float) and not isinstance(reported, bool) and reported > 0:
            parsed = replace(parsed, reported_cost=float(reported))
    return _max_usage(current, parsed)


def _extract_text_from_codex_jsonl(raw: str) -> tuple[str, _UsageTotals, str | None]:
    session_id: str | None = None
    usage_totals = _UsageTotals()
    turn_count = 0
    max_turn_input_tokens = 0
    messages: list[str] = []

    for event in _iter_json_events(raw):
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                session_id = thread_id
            continue

        if event.get("type") in {"turn.completed", "token_count"}:
            usage = event.get("usage" if event.get("type") == "turn.completed" else "info", {})
            if isinstance(usage, dict):
                turn_count += 1
                parsed = _usage_totals_from_dict(usage, input_includes_cache=True)
                max_turn_input_tokens = max(
                    max_turn_input_tokens,
                    parsed.max_turn_input_tokens,
                )
                usage_totals = _max_usage(usage_totals, parsed)
            continue

        if event.get("type") != "item.completed":
            continue
        item = event.get("item", {})
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str):
            messages.append(text)

    usage_totals = _UsageTotals(
        tokens_in=usage_totals.tokens_in,
        tokens_out=usage_totals.tokens_out,
        cached_tokens_in=usage_totals.cached_tokens_in,
        cache_write_tokens_in=usage_totals.cache_write_tokens_in,
        turn_count=turn_count,
        max_turn_input_tokens=max_turn_input_tokens,
    )
    return (messages[-1] if messages else raw), usage_totals, session_id


def _extract_text_from_grok_jsonl(raw: str) -> tuple[str, _UsageTotals, str | None]:
    """Parse Grok CLI JSONL output.  Delegates to the narrow Grok parser."""
    from agentshore.agents import cli_grok

    return cli_grok.parse_grok_jsonl(raw)


def _extract_text_from_stream_json(raw: str) -> str:
    last_result: str | None = None
    for event in _iter_json_events(raw):
        if event.get("type") == "result" and "result" in event:
            last_result = str(event["result"])
    if last_result is not None:
        return last_result

    parts: list[str] = []
    for event in _iter_json_events(raw):
        if event.get("type") == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", []) if isinstance(msg, dict) else []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
        elif event.get("type") == "content_block_delta":
            delta = event.get("delta", {})
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                parts.append(str(delta.get("text", "")))
    return "".join(parts)


def _parse_claude_output(raw: str) -> tuple[str, _UsageTotals, str | None]:
    """Parse a Claude Code stream-json transcript into text/usage/session id."""
    usage = _UsageTotals()
    for line in map(str.strip, raw.splitlines()):
        if line:
            usage = _maybe_parse_usage(line.encode("utf-8"), usage)
    return _extract_text_from_stream_json(raw), usage, _extract_session_id_from_jsonl(raw)


def _extract_text_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_extract_text_value(item) for item in value]
        return "".join(part for part in parts if part)
    if not isinstance(value, dict):
        return None

    for key in ("text", "content", "response", "result"):
        text = _extract_text_value(value.get(key))
        if text:
            return text

    value_parts = value.get("parts")
    if isinstance(value_parts, list):
        text_parts = [_extract_text_value(part) for part in value_parts]
        return "".join(part for part in text_parts if part)
    return None


# ---------------------------------------------------------------------------
# Parser protocol + registry
# ---------------------------------------------------------------------------


class CliOutputFormat(Protocol):
    """A per-agent-type parser: raw stdout -> (text, usage totals, session id)."""

    def parse(self, raw: str) -> tuple[str, _UsageTotals, str | None]: ...


@dataclass(frozen=True, slots=True)
class _FunctionFormat:
    """Adapt a free parse function into the :class:`CliOutputFormat` protocol."""

    _parse: Callable[[str], tuple[str, _UsageTotals, str | None]]

    def parse(self, raw: str) -> tuple[str, _UsageTotals, str | None]:
        return self._parse(raw)


# Registry: adding a fourth agent type is one entry here, not an if/elif edit
# in ``_read_output``.
_PARSERS: dict[AgentType, CliOutputFormat] = {
    AgentType.CLAUDE_CODE: _FunctionFormat(_parse_claude_output),
    AgentType.CODEX: _FunctionFormat(_extract_text_from_codex_jsonl),
    AgentType.GROK: _FunctionFormat(_extract_text_from_grok_jsonl),
}
