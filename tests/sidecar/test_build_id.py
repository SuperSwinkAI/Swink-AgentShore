"""Tests for the sidecar build-id resolver."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentshore.sidecar import build_id as build_id_module
from agentshore.sidecar.build_id import _DEV_BUILD, load_build_info


def test_unfrozen_returns_dev_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert load_build_info() == _DEV_BUILD


def test_frozen_with_valid_payload_returns_embedded_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = {
        "build_id": "abc123def456",
        "git_sha": "0123456789abcdef0123456789abcdef01234567",
        "built_at": "2026-05-15T10:00:00+00:00",
    }
    (tmp_path / "build_id.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    info = load_build_info()
    assert info == payload


def test_frozen_missing_payload_falls_back_to_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert load_build_info() == _DEV_BUILD


def test_frozen_corrupt_payload_falls_back_to_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "build_id.json").write_text("not json", encoding="utf-8")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert load_build_info() == _DEV_BUILD


def test_frozen_wrong_shape_falls_back_to_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "build_id.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert load_build_info() == _DEV_BUILD


def test_frozen_missing_field_falls_back_to_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "build_id.json").write_text(
        json.dumps({"build_id": "x", "git_sha": "y"}), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert load_build_info() == _DEV_BUILD


def test_bundle_root_helper_returns_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert build_id_module._bundle_root() is None
