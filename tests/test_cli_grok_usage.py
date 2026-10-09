"""Grok CLI parser/usage/first-byte/alias coverage (WI-A, issues #177/#204).

Grok 0.2.32 emitted *no* usage block, so that real capture asserts the parser
degrades gracefully (text + session id, zero usage, no error). The 1.0.50
capture asserts usage and the vendor-reported ``total_cost_usd`` are read, and
shape-variant unit tests cover older/relay usage shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.agents.cli_grok import (
    _grok_usage_block,
    _grok_usage_from_dict,
    parse_grok_jsonl,
)
from agentshore.config.models import AgentConfig
from agentshore.state import AgentType

_FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_real_0_2_32_capture_parses_without_usage() -> None:
    """Real grok 0.2.32 capture: text + session id resolve, usage stays zero."""
    raw = _read("grok_streaming_real_0_2_32.jsonl")
    text, usage, session_id = parse_grok_jsonl(raw)

    assert text == "Hi"
    assert session_id == "019ecfe0-b449-7cc2-a354-15e544d5f11f"
    # grok 0.2.32 emits no usage at all -> graceful zero, never an exception.
    assert usage.tokens_in == 0
    assert usage.tokens_out == 0


def test_with_usage_fixture_extracts_nonzero_tokens() -> None:
    """Representative with-usage fixture: widened parser yields non-zero tokens."""
    raw = _read("grok_streaming_with_usage.jsonl")
    text, usage, session_id = parse_grok_jsonl(raw)

    assert text == "Hello world"
    assert session_id == "019ecfe0-aaaa-bbbb-cccc-000000000001"
    assert usage.tokens_in == 120
    assert usage.tokens_out == 45
    assert usage.cached_tokens_in == 10
    assert usage.cache_write_tokens_in == 5


@pytest.mark.parametrize(
    ("usage", "expected_in", "expected_out"),
    [
        ({"input_tokens": 10, "output_tokens": 7}, 10, 7),
        ({"prompt_tokens": 33, "completion_tokens": 11}, 33, 11),
        ({"tokens_in": 5, "tokens_out": 9}, 5, 9),
        ({"input": 4, "output": 6}, 4, 6),
        ({"input_tokens": 8, "reasoning_output_tokens": 3}, 8, 3),
    ],
)
def test_usage_shape_variants(
    usage: dict[str, object], expected_in: int, expected_out: int
) -> None:
    totals = _grok_usage_from_dict(usage)
    assert totals.tokens_in == expected_in
    assert totals.tokens_out == expected_out


def test_usage_block_nesting_tolerance() -> None:
    """Usage nested under a relay parent key is still located."""
    assert _grok_usage_block({"usage": {"input_tokens": 1}}) == {"input_tokens": 1}
    assert _grok_usage_block({"result": {"usage": {"input_tokens": 2}}}) == {"input_tokens": 2}
    assert _grok_usage_block({"turn": {"usage": {"output_tokens": 3}}}) == {"output_tokens": 3}
    assert _grok_usage_block({"tokens_in": 4, "tokens_out": 5}) == {"tokens_in": 4, "tokens_out": 5}
    # No usage-bearing keys (the live 0.2.32 ``end`` event) -> None.
    assert _grok_usage_block({"stopReason": "EndTurn", "sessionId": "x"}) is None


def test_real_1_0_50_capture_reads_usage_and_vendor_cost() -> None:
    """grok 1.0.50 stamps ``total_cost_usd`` on ``end``; it wins over the pricing table."""
    text, usage, session_id = parse_grok_jsonl(_read("grok_streaming_real_1_0_50.jsonl"))

    assert text == "OK"
    assert session_id == "01a11e87-9f16-78c0-971a-8d8c0c382746"
    assert usage.tokens_in == 23393
    assert usage.tokens_out == 16
    assert usage.reported_cost == pytest.approx(0.01593988)


def test_error_event_surfaces_message_not_raw_json() -> None:
    """A pre-start failure (not signed in) returns the message, not the JSON line."""
    text, usage, session_id = parse_grok_jsonl(_read("grok_error_unauth_1_0_50.jsonl"))

    assert text.startswith("Not signed in.")
    assert '"type"' not in text
    assert session_id is None
    assert usage.reported_cost == 0.0


def test_first_byte_deadline_resolution() -> None:
    """Per-type default, config override, and global default all clamp to timeout."""
    from agentshore.agents.cli.supervisor import resolve_first_byte_deadline
    from agentshore.agents.cli.watchdogs import _FIRST_BYTE_DEADLINE_S

    cfg = AgentConfig()
    # All streaming agents share one generous 600s first-byte deadline (#213):
    # reasoning models legitimately go silent before the first token, so the
    # deadline only catches a child emitting nothing; wall-clock backstops hangs.
    assert resolve_first_byte_deadline(AgentType.GROK, cfg, timeout=3600.0) == 600.0
    assert (
        resolve_first_byte_deadline(AgentType.GROK, cfg, timeout=3600.0) == _FIRST_BYTE_DEADLINE_S
    )
    # Codex/other falls back to the same global default.
    assert (
        resolve_first_byte_deadline(AgentType.CODEX, cfg, timeout=3600.0) == _FIRST_BYTE_DEADLINE_S
    )
    # Explicit config override wins over the per-type default.
    cfg_override = AgentConfig(first_byte_timeout_seconds=20)
    assert resolve_first_byte_deadline(AgentType.GROK, cfg_override, timeout=3600.0) == 20.0
    # Always clamped to the wall-clock timeout.
    assert resolve_first_byte_deadline(AgentType.GROK, cfg, timeout=10.0) == 10.0
