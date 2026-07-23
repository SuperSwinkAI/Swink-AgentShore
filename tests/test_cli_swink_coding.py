"""Tests for the swink-coding CLI adapter: argv shape (prompt-argv/stdin/prompt-file,
yolo, --cwd, resume), NDJSON result/error parsing, and terminal-event detection.
"""

from __future__ import annotations

import pytest

from agentshore.agents.cli_swink_coding import (
    TIER_ALIASES,
    build_argv,
    build_resume_argv,
    classify_swink_model,
    parse_swink_coding_jsonl,
)

# ---------------------------------------------------------------------------
# build_argv — prompt delivery modes
# ---------------------------------------------------------------------------


def test_build_argv_default_prompt_argv_shape() -> None:
    argv = build_argv(
        prompt="do the thing",
        binary="swink-coding",
        model="small",
        reasoning_effort=None,
        extra_flags=("--yolo",),
        project_dir="/worktree",
        prompt_on_stdin=False,
    )
    assert argv == [
        "swink-coding",
        "--model",
        "small",
        "--yolo",
        "--output-format",
        "stream-json",
        "--cwd",
        "/worktree",
        "-p",
        "do the thing",
    ]


def test_build_argv_defaults_binary_when_omitted() -> None:
    argv = build_argv(
        prompt="hi",
        binary=None,
        model=None,
        reasoning_effort=None,
        extra_flags=(),
        project_dir=None,
        prompt_on_stdin=False,
    )
    assert argv[0] == "swink-coding"


def test_build_argv_omits_model_flag_when_none() -> None:
    argv = build_argv(
        prompt="hi",
        binary="swink-coding",
        model=None,
        reasoning_effort=None,
        extra_flags=(),
        project_dir=None,
        prompt_on_stdin=False,
    )
    assert "--model" not in argv


def test_build_argv_ignores_reasoning_effort() -> None:
    """No effort flag is registered for this agent type; the kwarg is accepted
    for signature parity with the other CLI adapters and otherwise ignored."""
    argv = build_argv(
        prompt="hi",
        binary="swink-coding",
        model="large",
        reasoning_effort="high",
        extra_flags=(),
        project_dir=None,
        prompt_on_stdin=False,
    )
    assert "--effort" not in argv
    assert "high" not in argv


def test_build_argv_prompt_on_stdin_omits_dash_p_entirely() -> None:
    huge = "x" * 20000
    argv = build_argv(
        prompt=huge,
        binary="swink-coding",
        model="medium",
        reasoning_effort=None,
        extra_flags=("--yolo",),
        project_dir="/wt",
        prompt_on_stdin=True,
    )
    assert "-p" not in argv
    assert huge not in argv


def test_build_argv_prompt_file_mode_uses_prompt_file_flag() -> None:
    argv = build_argv(
        prompt="ignored when prompt_file is set",
        binary="swink-coding",
        model="medium",
        reasoning_effort=None,
        extra_flags=("--yolo",),
        project_dir="/wt",
        prompt_on_stdin=True,
        prompt_file="/tmp/swink-coding-prompt.txt",
    )
    assert "--prompt-file" in argv
    assert argv[argv.index("--prompt-file") + 1] == "/tmp/swink-coding-prompt.txt"
    assert "-p" not in argv
    assert "ignored when prompt_file is set" not in argv


def test_build_argv_includes_yolo_flag() -> None:
    argv = build_argv(
        prompt="hi",
        binary="swink-coding",
        model="small",
        reasoning_effort=None,
        extra_flags=("--yolo",),
        project_dir=None,
        prompt_on_stdin=False,
    )
    assert "--yolo" in argv


def test_build_argv_includes_cwd_flag_when_project_dir_given() -> None:
    argv = build_argv(
        prompt="hi",
        binary="swink-coding",
        model="small",
        reasoning_effort=None,
        extra_flags=(),
        project_dir="/some/worktree",
        prompt_on_stdin=False,
    )
    assert "--cwd" in argv
    assert argv[argv.index("--cwd") + 1] == "/some/worktree"


def test_build_argv_omits_cwd_flag_when_project_dir_absent() -> None:
    argv = build_argv(
        prompt="hi",
        binary="swink-coding",
        model="small",
        reasoning_effort=None,
        extra_flags=(),
        project_dir=None,
        prompt_on_stdin=False,
    )
    assert "--cwd" not in argv


def test_build_argv_always_includes_output_format_stream_json() -> None:
    argv = build_argv(
        prompt="hi",
        binary="swink-coding",
        model="small",
        reasoning_effort=None,
        extra_flags=(),
        project_dir=None,
        prompt_on_stdin=False,
    )
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"


# ---------------------------------------------------------------------------
# build_resume_argv
# ---------------------------------------------------------------------------


def test_build_resume_argv_injects_resume_flag_after_binary() -> None:
    argv = build_resume_argv(
        resume_session_id="sc_abc123",
        prompt="emit the block",
        binary="swink-coding",
        model="small",
        reasoning_effort=None,
        extra_flags=("--yolo",),
        project_dir="/wt",
        prompt_on_stdin=False,
    )
    assert argv[:3] == ["swink-coding", "--resume", "sc_abc123"]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "small"
    assert argv[-2:] == ["-p", "emit the block"]


def test_build_resume_argv_uses_explicit_id_not_latest_sentinel() -> None:
    argv = build_resume_argv(
        resume_session_id="sc_specific",
        prompt="p",
        binary="swink-coding",
        model=None,
        reasoning_effort=None,
        extra_flags=(),
        project_dir=None,
        prompt_on_stdin=False,
    )
    assert "latest" not in argv
    assert "sc_specific" in argv


# ---------------------------------------------------------------------------
# classify_swink_model — tier alias vs. provider:model[@endpoint] tier_map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", sorted(TIER_ALIASES))
def test_classify_swink_model_accepts_tier_aliases(alias: str) -> None:
    assert classify_swink_model(alias) == "alias"


def test_classify_swink_model_accepts_colon_in_model_id() -> None:
    assert classify_swink_model("ollama:qwen3.5:4b") == "tier_map"
    assert classify_swink_model("ollama:qwen2.5-coder:7b") == "tier_map"


def test_classify_swink_model_accepts_endpoint_form() -> None:
    assert classify_swink_model("vllm:m@http://host:8000/v1") == "tier_map"


def test_classify_swink_model_accepts_bare_provider_model_pair() -> None:
    # Grammatically valid provider:model — acceptance at this layer is fine;
    # whether "qwen3.5" is a real provider is a config/catalog-layer concern.
    assert classify_swink_model("qwen3.5:4b") == "tier_map"


def test_classify_swink_model_at_without_url_scheme_folds_into_model() -> None:
    # No "://" after '@' — not treated as an endpoint, so it's still a valid
    # (if unusual) model id and classifies as tier_map.
    assert classify_swink_model("ollama:q@ver") == "tier_map"


def test_classify_swink_model_rejects_bare_model_with_no_provider() -> None:
    with pytest.raises(ValueError, match="tier alias.*provider:model"):
        classify_swink_model("gpt-4")


def test_classify_swink_model_rejects_empty_provider() -> None:
    with pytest.raises(ValueError, match="tier alias.*provider:model"):
        classify_swink_model(":model")


def test_classify_swink_model_rejects_empty_model() -> None:
    with pytest.raises(ValueError, match="tier alias.*provider:model"):
        classify_swink_model("ollama:")


# ---------------------------------------------------------------------------
# build_argv / build_resume_argv — tier_map dispatch shape
# ---------------------------------------------------------------------------


def test_build_argv_alias_model_unaffected_by_model_tier_kwarg() -> None:
    argv = build_argv(
        prompt="hi",
        binary="swink-coding",
        model="small",
        reasoning_effort=None,
        extra_flags=(),
        project_dir=None,
        prompt_on_stdin=False,
        model_tier="medium",  # irrelevant for a plain alias; ignored
    )
    assert argv[1:3] == ["--model", "small"]
    assert "--tier-map" not in argv


def test_build_argv_tier_map_model_emits_model_and_tier_map_flags() -> None:
    argv = build_argv(
        prompt="do the thing",
        binary="swink-coding",
        model="ollama:qwen2.5-coder:7b",
        reasoning_effort=None,
        extra_flags=("--yolo",),
        project_dir="/worktree",
        prompt_on_stdin=False,
        model_tier="small",
    )
    assert argv == [
        "swink-coding",
        "--model",
        "small",
        "--tier-map",
        "small=ollama:qwen2.5-coder:7b",
        "--yolo",
        "--output-format",
        "stream-json",
        "--cwd",
        "/worktree",
        "-p",
        "do the thing",
    ]


def test_build_argv_tier_map_model_without_model_tier_raises() -> None:
    with pytest.raises(ValueError, match="requires model_tier"):
        build_argv(
            prompt="hi",
            binary="swink-coding",
            model="ollama:qwen2.5-coder:7b",
            reasoning_effort=None,
            extra_flags=(),
            project_dir=None,
            prompt_on_stdin=False,
        )


def test_build_argv_tier_map_model_with_invalid_model_tier_raises() -> None:
    with pytest.raises(ValueError, match="requires model_tier"):
        build_argv(
            prompt="hi",
            binary="swink-coding",
            model="ollama:qwen2.5-coder:7b",
            reasoning_effort=None,
            extra_flags=(),
            project_dir=None,
            prompt_on_stdin=False,
            model_tier="not-a-tier",
        )


def test_build_resume_argv_tier_map_model_emits_model_and_tier_map_flags() -> None:
    argv = build_resume_argv(
        resume_session_id="sc_abc123",
        prompt="emit the block",
        binary="swink-coding",
        model="ollama:qwen2.5-coder:7b",
        reasoning_effort=None,
        extra_flags=("--yolo",),
        project_dir="/wt",
        prompt_on_stdin=False,
        model_tier="small",
    )
    assert argv[:3] == ["swink-coding", "--resume", "sc_abc123"]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "small"
    assert "--tier-map" in argv
    assert argv[argv.index("--tier-map") + 1] == "small=ollama:qwen2.5-coder:7b"


# ---------------------------------------------------------------------------
# parse_swink_coding_jsonl — result event (primary path)
# ---------------------------------------------------------------------------


def test_parse_result_event_is_authoritative_over_text_deltas() -> None:
    raw = "\n".join(
        [
            '{"type":"session.started","session_id":"sc_1","tier":"small","context_window":32768}',
            '{"type":"text","data":"partial "}',
            '{"type":"text","data":"delta"}',
            '{"type":"tool_use","name":"bash","input":{"command":"ls"}}',
            '{"type":"tool_result","name":"bash","ok":true}',
            (
                '{"type":"result","text":"FULL final text","tier":"small",'
                '"context_window":32768,"session_id":"sc_1",'
                '"usage":{"input_tokens":100,"output_tokens":40,'
                '"cache_read_input_tokens":10,"cache_creation_input_tokens":5,'
                '"reasoning_output_tokens":0},'
                '"duration_ms":1200,"time_to_first_byte_ms":300}'
            ),
        ]
    )
    text, usage, session_id = parse_swink_coding_jsonl(raw)

    assert text == "FULL final text"
    assert session_id == "sc_1"
    # tokens_in follows the same input_includes_cache=False convention as the
    # Claude parser: input_tokens + cache_read + cache_write (100 + 10 + 5).
    assert usage.tokens_in == 115
    assert usage.tokens_out == 40
    assert usage.cached_tokens_in == 10
    assert usage.cache_write_tokens_in == 5


def test_parse_result_event_session_id_from_session_started_when_result_lacks_one() -> None:
    raw = "\n".join(
        [
            '{"type":"session.started","session_id":"sc_from_start"}',
            '{"type":"result","text":"done"}',
        ]
    )
    text, _usage, session_id = parse_swink_coding_jsonl(raw)
    assert text == "done"
    assert session_id == "sc_from_start"


def test_parse_result_event_empty_text_is_authoritative_not_overridden_by_deltas() -> None:
    """An ``"empty":true`` result must win even though earlier text deltas exist —
    the terminal result event is authoritative, deltas are fallback-only."""
    raw = "\n".join(
        [
            '{"type":"text","data":"some partial output"}',
            '{"type":"result","text":"","session_id":"sc_2","empty":true}',
        ]
    )
    text, _usage, session_id = parse_swink_coding_jsonl(raw)
    assert text == ""
    assert session_id == "sc_2"


def test_parse_result_event_reasoning_tokens_used_when_no_output_tokens() -> None:
    raw = (
        '{"type":"result","text":"x","usage":{"input_tokens":8,'
        '"output_tokens":0,"reasoning_output_tokens":3}}'
    )
    _text, usage, _session_id = parse_swink_coding_jsonl(raw)
    assert usage.tokens_in == 8
    assert usage.tokens_out == 3


# ---------------------------------------------------------------------------
# parse_swink_coding_jsonl — error event (failure fallback)
# ---------------------------------------------------------------------------


def test_parse_error_event_surfaces_message_when_no_result_arrived() -> None:
    raw = "\n".join(
        [
            '{"type":"session.started","session_id":"sc_3"}',
            '{"type":"tool_use","name":"bash","input":{}}',
            '{"type":"error","message":"model backend unreachable"}',
        ]
    )
    text, usage, session_id = parse_swink_coding_jsonl(raw)
    assert text == "model backend unreachable"
    assert session_id == "sc_3"
    assert usage.tokens_in == 0
    assert usage.tokens_out == 0


def test_parse_result_event_wins_over_a_preceding_error_event() -> None:
    """Defensive: if a result event does arrive, it is authoritative even if an
    error event was also observed earlier in the stream."""
    raw = "\n".join(
        [
            '{"type":"error","message":"transient retry notice"}',
            '{"type":"result","text":"recovered and finished"}',
        ]
    )
    text, _usage, _session_id = parse_swink_coding_jsonl(raw)
    assert text == "recovered and finished"


# ---------------------------------------------------------------------------
# parse_swink_coding_jsonl — text-delta fallback (neither result nor error)
# ---------------------------------------------------------------------------


def test_parse_falls_back_to_concatenated_text_deltas_when_no_terminal_event() -> None:
    raw = "\n".join(
        [
            '{"type":"session.started","session_id":"sc_4"}',
            '{"type":"text","data":"Hello "}',
            '{"type":"text","data":"world"}',
        ]
    )
    text, _usage, session_id = parse_swink_coding_jsonl(raw)
    assert text == "Hello world"
    assert session_id == "sc_4"


def test_parse_falls_back_to_raw_when_nothing_recognisable() -> None:
    raw = "not even json\nmore garbage"
    text, usage, session_id = parse_swink_coding_jsonl(raw)
    assert text == raw
    assert usage.tokens_in == 0
    assert session_id is None


# ---------------------------------------------------------------------------
# Registration: _PARSERS / _TERMINAL_EVENT_TYPES / _FIRST_BYTE_DEADLINE_BY_TYPE
# ---------------------------------------------------------------------------


def test_swink_coding_registered_in_parsers_and_terminal_events_and_watchdog() -> None:
    from agentshore.agents.cli.parsing import _PARSERS, _TERMINAL_EVENT_TYPES, _is_terminal_event
    from agentshore.agents.cli.watchdogs import _FIRST_BYTE_DEADLINE_BY_TYPE
    from agentshore.state import AgentType

    assert AgentType.SWINK_CODING in _PARSERS
    assert _TERMINAL_EVENT_TYPES[AgentType.SWINK_CODING] == frozenset({"result"})
    # 60s spawn headroom only: swink-coding >= 0.2.0 contractually flushes
    # session.started before the first backend request (SuperSwink-Coding#278).
    assert _FIRST_BYTE_DEADLINE_BY_TYPE[AgentType.SWINK_CODING] == 60.0

    terminal_line = b'{"type":"result","text":"done"}'
    assert _is_terminal_event(terminal_line, AgentType.SWINK_CODING) is True
    non_terminal_line = b'{"type":"text","data":"partial"}'
    assert _is_terminal_event(non_terminal_line, AgentType.SWINK_CODING) is False


def test_swink_coding_registered_as_resumable_and_yolo_default() -> None:
    from agentshore.agents.cli.argv import _DEFAULT_YOLO_FLAGS, _RESUMABLE_AGENT_TYPES
    from agentshore.state import AgentType

    assert AgentType.SWINK_CODING in _RESUMABLE_AGENT_TYPES
    assert _DEFAULT_YOLO_FLAGS[AgentType.SWINK_CODING] == ("--yolo",)


# ---------------------------------------------------------------------------
# Top-level dispatch (agentshore.agents.cli.argv.build_argv/build_resume_argv)
# ---------------------------------------------------------------------------


def test_top_level_build_argv_dispatches_to_swink_coding_adapter() -> None:
    from agentshore.agents.cli.argv import build_argv as dispatch_build_argv
    from agentshore.state import AgentType

    argv = dispatch_build_argv(
        AgentType.SWINK_CODING,
        "do the thing",
        binary="swink-coding",
        model="large",
        project_dir="/wt",
    )
    assert argv[0] == "swink-coding"
    assert "--yolo" in argv  # applied YOLO default since no extra_flags given
    assert "--model" in argv and argv[argv.index("--model") + 1] == "large"
    assert "--cwd" in argv and argv[argv.index("--cwd") + 1] == "/wt"
    assert argv[-2:] == ["-p", "do the thing"]


def test_top_level_build_resume_argv_dispatches_to_swink_coding_adapter() -> None:
    from agentshore.agents.cli.argv import build_resume_argv as dispatch_build_resume_argv
    from agentshore.state import AgentType

    argv = dispatch_build_resume_argv(
        AgentType.SWINK_CODING,
        "emit the block",
        "sc_resume_1",
        binary="swink-coding",
        model="large",
        project_dir="/wt",
    )
    assert argv[:3] == ["swink-coding", "--resume", "sc_resume_1"]


def test_top_level_build_argv_threads_model_tier_to_swink_coding_adapter() -> None:
    from agentshore.agents.cli.argv import build_argv as dispatch_build_argv
    from agentshore.state import AgentType

    argv = dispatch_build_argv(
        AgentType.SWINK_CODING,
        "do the thing",
        binary="swink-coding",
        model="ollama:qwen2.5-coder:7b",
        project_dir="/wt",
        model_tier="small",
    )
    assert "--tier-map" in argv
    assert argv[argv.index("--tier-map") + 1] == "small=ollama:qwen2.5-coder:7b"
    assert argv[argv.index("--model") + 1] == "small"
