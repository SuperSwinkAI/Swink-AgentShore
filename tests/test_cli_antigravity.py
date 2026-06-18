"""Tests for the Antigravity (``agy``) CLI adapter: output unwrap, conversation-id
resolution, and the JSON-retry resume shape (desktop-dy2j)."""

from __future__ import annotations

import json
from pathlib import Path

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
