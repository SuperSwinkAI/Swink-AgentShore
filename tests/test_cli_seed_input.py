from __future__ import annotations

from pathlib import Path

import click
import pytest

from agentshore.cli import _resolve_seed_input_path


def test_resolve_seed_input_path_accepts_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    seed_file = tmp_path / "seed.md"
    seed_file.write_text("# Seed\n", encoding="utf-8")

    resolved, kind = _resolve_seed_input_path(str(seed_file), repo_root)

    assert resolved == seed_file
    assert kind == "file"


def test_resolve_seed_input_path_bundles_directory(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    seed_dir = tmp_path / "seed-dir"
    seed_dir.mkdir()
    (seed_dir / "PRD.md").write_text("hello\n", encoding="utf-8")
    (seed_dir / "nested").mkdir()
    (seed_dir / "nested" / "notes.txt").write_text("world\n", encoding="utf-8")

    resolved, kind = _resolve_seed_input_path(str(seed_dir), repo_root)

    assert kind == "directory"
    assert resolved.exists()
    text = resolved.read_text(encoding="utf-8")
    assert "Source directory:" in text
    assert "PRD.md" in text
    assert "nested/notes.txt" in text


def test_resolve_seed_input_path_rejects_missing_path(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(click.BadParameter, match="Seed path does not exist"):
        _resolve_seed_input_path(str(tmp_path / "missing"), repo_root)


def test_resolve_seed_input_path_rejects_directory_without_supported_files(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    seed_dir = tmp_path / "seed-dir"
    seed_dir.mkdir()
    (seed_dir / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (seed_dir / "notes.log").write_text("plain text but unsupported suffix\n", encoding="utf-8")

    with pytest.raises(click.BadParameter, match="Seed directory has no supported files"):
        _resolve_seed_input_path(str(seed_dir), repo_root)


def test_resolve_seed_input_path_rejects_directory_without_utf8_readable_files(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    seed_dir = tmp_path / "seed-dir"
    seed_dir.mkdir()
    (seed_dir / "seed.md").write_bytes(b"\xff\xfe\x00\x00")

    with pytest.raises(click.BadParameter, match="Seed directory has no readable UTF-8 files"):
        _resolve_seed_input_path(str(seed_dir), repo_root)
