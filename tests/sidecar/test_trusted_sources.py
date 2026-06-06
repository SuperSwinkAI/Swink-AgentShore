"""Tests for no-auth trusted-source management (trusted_ids.github_logins).

Covers the sidecar functions that back the desktop "Trusted sources" panel:
login-only identities trusted as issue/PR sources but never assigned to an
agent. The list is the same ``trusted_ids.github_logins`` the CLI's
``agentshore trusted-ids`` group manages.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentshore.sidecar.identities import (
    add_trusted_source,
    list_trusted_sources,
    remove_trusted_source,
)
from agentshore.sidecar.server import INVALID_PARAMS, handle_request


def _write_config(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_add_list_remove_round_trip(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_config(cfg, {"identities": {}})

    assert list_trusted_sources(tmp_path) == []

    add_trusted_source(tmp_path, "OctoCat")
    add_trusted_source(tmp_path, "dependabot[bot]")
    assert list_trusted_sources(tmp_path) == ["dependabot[bot]", "octocat"]

    # Written canonical (lower-cased) into trusted_ids.github_logins.
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["trusted_ids"]["github_logins"] == ["octocat", "dependabot[bot]"]

    remove_trusted_source(tmp_path, "OCTOCAT")
    assert list_trusted_sources(tmp_path) == ["dependabot[bot]"]


def test_add_is_idempotent(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_config(cfg, {})
    add_trusted_source(tmp_path, "octocat")
    add_trusted_source(tmp_path, "OctoCat")  # same login, different case
    assert list_trusted_sources(tmp_path) == ["octocat"]
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["trusted_ids"]["github_logins"] == ["octocat"]


def test_preserves_pr_allow_list_and_restrict_flag(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_config(
        cfg,
        {
            "trusted_ids": {
                "github_logins": ["existing"],
                "pr_allow_list": [12, 34],
                "restrict_issues_to_trusted_authors": True,
            },
        },
    )

    add_trusted_source(tmp_path, "newbot")
    remove_trusted_source(tmp_path, "existing")

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["trusted_ids"]["github_logins"] == ["newbot"]
    # The neighbouring keys are left intact.
    assert data["trusted_ids"]["pr_allow_list"] == [12, 34]
    assert data["trusted_ids"]["restrict_issues_to_trusted_authors"] is True


def test_list_missing_trusted_ids_returns_empty(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_config(cfg, {"identities": {"a": {"gh_token_login": "a"}}})
    assert list_trusted_sources(tmp_path) == []


def test_list_missing_config_returns_empty(tmp_path: Path) -> None:
    assert list_trusted_sources(tmp_path) == []


def test_add_invalid_login_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_config(cfg, {})
    with pytest.raises(ValueError, match="invalid GitHub login"):
        add_trusted_source(tmp_path, "not a valid login!!")


def test_remove_missing_is_noop(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_config(cfg, {"trusted_ids": {"github_logins": ["keep"]}})
    remove_trusted_source(tmp_path, "absent")
    assert list_trusted_sources(tmp_path) == ["keep"]


def test_rpc_add_trusted_missing_login_returns_invalid_params() -> None:
    response = handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "identities.add_trusted", "params": {}}
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_rpc_remove_trusted_missing_login_returns_invalid_params() -> None:
    response = handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "identities.remove_trusted", "params": {}}
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS
