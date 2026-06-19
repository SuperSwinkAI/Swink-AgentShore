"""Shared JSONL / usage-accounting primitives for the CLI agent adapters.

The CLI agents (Claude Code, Codex, Grok) all emit JSONL on stdout and
share the same token-usage bookkeeping. These primitives used to live in
``cli_agent``; ``cli_grok`` imported them from there while ``cli_agent``
lazily imported ``cli_grok`` back — a circular edge that forced two
lazy-import wrappers (issue: TNQA finding #6). Hoisting the shared pieces into
this leaf module breaks the cycle: both adapters import from here, and neither
imports the other for these helpers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True, slots=True)
class _UsageTotals:
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens_in: int = 0
    cache_write_tokens_in: int = 0
    turn_count: int = 0
    max_turn_input_tokens: int = 0
    # Vendor-authoritative dollar cost when the agent reports one (Claude Code's
    # ``total_cost_usd`` on the result event). 0.0 means "agent reported no cost"
    # — the dispatch layer then derives cost from the token counts above. This
    # is preferred over token-derivation because the vendor figure accounts for
    # the exact model and 5-minute vs 1-hour ephemeral-cache tiers, which the
    # static pricing table cannot reconstruct from token counts alone.
    reported_cost: float = 0.0


def _iter_json_events(raw: str) -> Iterator[dict[str, object]]:
    """Yield each non-blank, JSON-decodable line of *raw* as a dict event.

    The CLI agents all emit JSONL on stdout; this is the single scan loop they
    share (skip blank lines, ``json.loads``, drop ``JSONDecodeError`` and
    non-dict payloads) so the per-format parsers only express their own event
    semantics.
    """
    for line in map(str.strip, raw.splitlines()):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _usage_totals_from_dict(
    usage: dict[str, object], *, input_includes_cache: bool
) -> _UsageTotals:
    total_usage = usage.get("total_token_usage")
    last_usage = usage.get("last_token_usage")
    turn_usage: dict[str, object] | None = None
    if isinstance(total_usage, dict):
        if isinstance(last_usage, dict):
            turn_usage = last_usage
        usage = total_usage
        input_includes_cache = True
    elif isinstance(last_usage, dict):
        usage = last_usage
        turn_usage = last_usage
        input_includes_cache = True

    input_tokens = _first_int(usage, "input_tokens")
    cache_read_tokens = _safe_int(usage.get("cached_input_tokens")) + _safe_int(
        usage.get("cache_read_input_tokens")
    )
    cache_write_tokens = _first_int(usage, "cache_creation_input_tokens")
    output_tokens = _first_int(usage, "output_tokens")
    reasoning_tokens = _first_int(usage, "reasoning_output_tokens")

    tokens_in = input_tokens if input_includes_cache else input_tokens + cache_read_tokens
    if not input_includes_cache:
        tokens_in += cache_write_tokens

    tokens_out = output_tokens if output_tokens > 0 else reasoning_tokens
    max_turn_input_tokens = _safe_int(turn_usage.get("input_tokens")) if turn_usage else tokens_in
    return _UsageTotals(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cached_tokens_in=cache_read_tokens,
        cache_write_tokens_in=cache_write_tokens,
        max_turn_input_tokens=max_turn_input_tokens,
    )


def _max_usage(left: _UsageTotals, right: _UsageTotals) -> _UsageTotals:
    return _UsageTotals(
        tokens_in=max(left.tokens_in, right.tokens_in),
        tokens_out=max(left.tokens_out, right.tokens_out),
        cached_tokens_in=max(left.cached_tokens_in, right.cached_tokens_in),
        cache_write_tokens_in=max(left.cache_write_tokens_in, right.cache_write_tokens_in),
        turn_count=max(left.turn_count, right.turn_count),
        max_turn_input_tokens=max(left.max_turn_input_tokens, right.max_turn_input_tokens),
        reported_cost=max(left.reported_cost, right.reported_cost),
    )


def _first_int(values: dict[str, object], *keys: str) -> int:
    for key in keys:
        parsed = _safe_int(values.get(key))
        if parsed:
            return parsed
    return 0


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0
