"""Tests for the Antigravity (``agy``) CLI adapter: output unwrap, conversation-id
resolution, and the JSON-retry resume shape (desktop-dy2j)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentshore.agents import cli_antigravity


def _write_cache(home: Path, mapping: dict[str, str]) -> None:
    cache_dir = home / ".gemini" / "antigravity-cli" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "last_conversations.json").write_text(json.dumps(mapping), encoding="utf-8")


def test_resolve_conversation_id_hit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    wt = "/Users/x/wt/agentshore-1"
    _write_cache(home, {wt: "conv-uuid-1", "/other": "conv-uuid-2"})
    assert cli_antigravity.resolve_conversation_id(wt, home=str(home)) == "conv-uuid-1"


def test_resolve_conversation_id_accepts_path_object(tmp_path: Path) -> None:
    home = tmp_path / "home"
    wt = tmp_path / "wt"
    _write_cache(home, {str(wt): "conv-uuid-3"})
    assert cli_antigravity.resolve_conversation_id(wt, home=str(home)) == "conv-uuid-3"


def test_resolve_conversation_id_miss(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_cache(home, {"/some/other/dir": "conv-uuid-1"})
    assert cli_antigravity.resolve_conversation_id("/not/in/cache", home=str(home)) is None


def test_resolve_conversation_id_missing_file(tmp_path: Path) -> None:
    # home exists but no cache file at all.
    assert cli_antigravity.resolve_conversation_id("/wt", home=str(tmp_path)) is None


def test_resolve_conversation_id_malformed_json(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cache_dir = home / ".gemini" / "antigravity-cli" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "last_conversations.json").write_text("{not valid json", encoding="utf-8")
    assert cli_antigravity.resolve_conversation_id("/wt", home=str(home)) is None


def test_resolve_conversation_id_non_dict_payload(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cache_dir = home / ".gemini" / "antigravity-cli" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "last_conversations.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert cli_antigravity.resolve_conversation_id("/wt", home=str(home)) is None


def test_resolve_conversation_id_empty_or_nonstring_value(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_cache(home, {"/wt-empty": ""})
    assert cli_antigravity.resolve_conversation_id("/wt-empty", home=str(home)) is None
    # a non-string value (defensive) also resolves to None
    cache = home / ".gemini" / "antigravity-cli" / "cache" / "last_conversations.json"
    cache.write_text(json.dumps({"/wt-num": 123}), encoding="utf-8")
    assert cli_antigravity.resolve_conversation_id("/wt-num", home=str(home)) is None


def test_build_resume_argv_injects_conversation_flag() -> None:
    argv = cli_antigravity.build_resume_argv(
        resume_session_id="conv-uuid-9",
        prompt="emit the block",
        binary="agy",
        model="Gemini 3.5 Flash (Low)",
        reasoning_effort=None,
        extra_flags=("--dangerously-skip-permissions",),
        project_dir="/wt",
        prompt_on_stdin=False,
    )
    assert argv[:3] == ["agy", "--conversation", "conv-uuid-9"]
    assert "--add-dir" in argv and argv[argv.index("--add-dir") + 1] == "/wt"
    assert argv[-2:] == ["-p", "emit the block"]


def test_is_async_handoff_detects_manage_task_marker() -> None:
    # #236: original variant — delegated to the internal manage_task async tool.
    raw = (
        "Obtaining command output... To check progress: `manage_task status "
        "0aaf5ef8-1242-46f9-ba7e-41bf3dcb47d0/task-14` or wait for notification."
    )
    assert cli_antigravity.is_async_handoff(raw) is True


def test_is_async_handoff_detects_background_task_wait() -> None:
    # #236 resurfacing (session aa0b28cd): same behaviour, no manage_task token —
    # the agent paused and waited on a backgrounded command instead.
    raw = (
        "I will run cargo clippy with the isolated target directory to check for any "
        "errors or warnings. I will pause calling tools and wait for the cargo clippy "
        "background task to finish."
    )
    assert cli_antigravity.is_async_handoff(raw) is True


def test_is_async_handoff_is_case_insensitive() -> None:
    assert cli_antigravity.is_async_handoff("Pause Calling Tools and wait.") is True


def test_is_async_handoff_false_for_completed_work() -> None:
    # A normal terminal turn that emitted a result block must not be misclassified.
    raw = '```json\n{"success": true, "summary": "opened PR #42"}\n```'
    assert cli_antigravity.is_async_handoff(raw) is False


# #242: real-world tails the ORIGINAL markers all missed (0/16 detected). Each must
# now classify as an async handoff so the play gets the synchronous re-run nudge.
@pytest.mark.parametrize(
    "tail",
    [
        "it will complete in the background and we will be notified upon its completion. "
        "I will now wait for the background build to finish.",
        "I am waiting for the background task running `bash scripts/test.sh` to complete.",
        "I've started the build script in the background. I will wait for it to finish.",
        "I will wait for it to complete. Let's wait for the task to complete.",
        "I will now pause and wait for the system to notify me when it is done.",
        "Since it was automatically sent to the background by the system, I will stop "
        "calling tools and wait for the system to notify me when it finishes.",
        "I have started the tests in the background. I will wait for them to complete.",
    ],
)
def test_is_async_handoff_detects_real_242_phrasings(tail: str) -> None:
    assert cli_antigravity.is_async_handoff(tail) is True


# --- ensure_low_verbosity_setting --------------------------------------------


def _settings_path(home: Path) -> Path:
    return home / ".gemini" / "antigravity-cli" / "settings.json"


def test_ensure_low_verbosity_writes_when_absent(tmp_path: Path) -> None:
    # No settings file yet → creates dirs + file with verbosity: low.
    assert cli_antigravity.ensure_low_verbosity_setting(home=str(tmp_path)) is True
    data = json.loads(_settings_path(tmp_path).read_text())
    assert data == {"verbosity": "low"}


def test_ensure_low_verbosity_preserves_existing_keys(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"colorScheme": "dark", "model": "Gemini 3.1 Pro (High)"}))
    assert cli_antigravity.ensure_low_verbosity_setting(home=str(tmp_path)) is True
    data = json.loads(path.read_text())
    assert data["verbosity"] == "low"
    assert data["colorScheme"] == "dark"
    assert data["model"] == "Gemini 3.1 Pro (High)"


def test_ensure_low_verbosity_respects_existing_value(tmp_path: Path) -> None:
    # A user who set verbosity explicitly must not be overwritten.
    path = _settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"verbosity": "high"}))
    assert cli_antigravity.ensure_low_verbosity_setting(home=str(tmp_path)) is False
    assert json.loads(path.read_text())["verbosity"] == "high"


def test_ensure_low_verbosity_tolerates_malformed_file(tmp_path: Path) -> None:
    # Malformed JSON → start from an empty object, still write the setting.
    path = _settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert cli_antigravity.ensure_low_verbosity_setting(home=str(tmp_path)) is True
    assert json.loads(path.read_text()) == {"verbosity": "low"}


# --- strip_ansi (ConPTY terminal-escape cleanup) -----------------------------


def test_strip_ansi_removes_agy_conpty_prelude() -> None:
    # The exact prelude observed from agy under a ConPTY before its real output:
    # window-title stack, Device-Attributes query, focus reporting, win32 input.
    raw = "\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001hPONG"
    assert cli_antigravity.strip_ansi(raw) == "PONG"


def test_strip_ansi_strips_colour_and_cursor_codes() -> None:
    raw = "\x1b[2J\x1b[H\x1b[31mhello\x1b[0m world"
    assert cli_antigravity.strip_ansi(raw) == "hello world"


def test_strip_ansi_strips_osc_title_sequence() -> None:
    raw = "\x1b]0;some title\x07done"
    assert cli_antigravity.strip_ansi(raw) == "done"


def test_strip_ansi_normalises_crlf_and_lone_cr() -> None:
    assert cli_antigravity.strip_ansi("a\r\nb\rc\n") == "a\nb\nc\n"


def test_strip_ansi_is_noop_on_clean_text() -> None:
    clean = '```json\n{"success": true}\n```'
    assert cli_antigravity.strip_ansi(clean) == clean


def test_extract_output_strips_ansi_then_unwraps_task_block() -> None:
    # A ConPTY stream wraps the real output in a task-status block AND carries a
    # terminal prelude: extract_output must clean escapes first, then unwrap.
    raw = (
        "\x1b[1t\x1b[c\x1b[?9001h[Task abc/task-1 Status Update]\r\n"
        "Status: COMPLETED\r\n"
        "Exit Code: 0\r\n"
        "Output:\r\n"
        '```json\r\n{"success": true}\r\n```\r\n'
        "Error: (none)\r\n"
    )
    result = cli_antigravity.extract_output(raw)
    assert "\x1b" not in result
    assert "\r" not in result
    assert result == '```json\n{"success": true}\n```'
