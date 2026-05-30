from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from agentshore.agents.identity import IdentityResolutionError
from agentshore.errors import AgentAuthError
from agentshore.sidecar.identities import (
    add_identity,
    list_identities,
    remove_identity,
    update_identity,
)
from agentshore.sidecar.server import INVALID_PARAMS, handle_request


def _write_minimal_config(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "budget": {"enabled": True, "total": 20.0},
                "agents": {"codex": {"enabled": True, "identity": "oldlogin"}},
                "identities": {
                    "oldlogin": {
                        "git_user_name": "Old Login",
                        "git_user_email": "old@example.com",
                        "gh_token_env": "OLDLOGIN_GH_TOKEN",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_identity_config(path: Path, login: str, token_field: str, token_value: str) -> None:
    path.write_text(
        yaml.safe_dump({"identities": {login: {token_field: token_value}}}, sort_keys=False),
        encoding="utf-8",
    )


def test_identities_crud_write_through(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)

    add_identity(tmp_path, "NewLogin", "gh_token_login")
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "newlogin" in data["identities"]
    assert data["identities"]["newlogin"]["gh_token_login"] == "newlogin"

    update_identity(tmp_path, "newlogin", {"token_source": "gh_token_env"})
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "gh_token_env" in data["identities"]["newlogin"]
    assert "gh_token_login" not in data["identities"]["newlogin"]

    remove_identity(tmp_path, "oldlogin")
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "oldlogin" not in data["identities"]
    assert "identity" not in data["agents"]["codex"]


def test_identities_list_reports_env_missing(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)
    monkeypatch.delenv("OLDLOGIN_GH_TOKEN", raising=False)
    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "oldlogin",
            "source": "gh_token_env",
            "token_status": "missing",
            "repo_access": "unknown",
        }
    ]


def test_rpc_rejects_invalid_params() -> None:
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "identities.add", "params": {}})
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_add_identity_rejects_invalid_login(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)

    for bad_login in ("", "has space", "-starts-with-dash", "a" * 40, "double--hyphen"):
        with pytest.raises(ValueError, match="invalid.*login|login.*invalid"):
            add_identity(tmp_path, bad_login, "gh_token_login")


def test_add_identity_accepts_bot_login(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)
    add_identity(tmp_path, "mybot[bot]", "gh_token_login")
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "mybot[bot]" in data["identities"]


def test_update_identity_rejects_unknown_patch_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)

    with pytest.raises(ValueError, match="unknown.*patch|patch.*unknown"):
        update_identity(tmp_path, "oldlogin", {"gh_token_eenv": "typo"})


def test_update_identity_rejects_multiple_unknown_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)

    with pytest.raises(ValueError, match="unknown.*patch|patch.*unknown"):
        update_identity(tmp_path, "oldlogin", {"ssh_key_path": "/tmp/key", "bad_key": "oops"})


def test_update_identity_accepts_all_known_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)
    update_identity(
        tmp_path,
        "oldlogin",
        {
            "token_source": "gh_token_login",
            "git_user_name": "New Name",
            "git_user_email": "new@example.com",
            "gh_config_dir": "/home/user/.config/gh",
            "ssh_key_path": "/home/user/.ssh/id_ed25519",
        },
    )
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["identities"]["oldlogin"]["git_user_name"] == "New Name"


def test_rpc_update_identity_unknown_key_returns_invalid_params(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "identities.update",
            "params": {
                "path": str(tmp_path),
                "login": "oldlogin",
                "patch": {"gh_token_eenv": "typo"},
            },
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_rpc_add_identity_invalid_login_returns_invalid_params(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "identities.add",
            "params": {
                "path": str(tmp_path),
                "login": "has space",
                "token_source": "gh_token_login",
            },
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_identities_list_gh_login_does_not_fall_back_to_ambient(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "agentshore.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "identities": {
                    "oldlogin": {
                        "gh_token_login": "oldlogin",
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GH_TOKEN", "ambient-token")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-token")

    monkeypatch.setattr(
        "agentshore.sidecar.identities.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=1, stdout=""),
    )

    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "oldlogin",
            "source": "gh_token_login",
            "token_status": "missing",
            "repo_access": "unknown",
        }
    ]


def test_identities_list_gh_login_uses_resolved_token(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "agentshore.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "identities": {
                    "oldlogin": {
                        "gh_token_login": "oldlogin",
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "agentshore.sidecar.identities.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="resolved-token\n"
        ),
    )
    seen_env: dict[str, str] = {}

    def _verify(_project_path: Path, identity_env: dict[str, str]) -> None:
        seen_env.update(identity_env)

    monkeypatch.setattr("agentshore.sidecar.identities.verify_identity_repo_access", _verify)

    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "oldlogin",
            "source": "gh_token_login",
            "token_status": "configured",
            "repo_access": "ok",
        }
    ]
    assert seen_env == {"GH_TOKEN": "resolved-token", "GITHUB_TOKEN": "resolved-token"}


def test_list_identities_gh_token_login_happy_path(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_login", "newlogin")
    monkeypatch.setattr(
        "agentshore.sidecar.identities.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="resolved-token\n"
        ),
    )
    monkeypatch.setattr(
        "agentshore.sidecar.identities.verify_identity_repo_access",
        lambda _project_path, _identity_env: None,
    )

    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "newlogin",
            "source": "gh_token_login",
            "token_status": "configured",
            "repo_access": "ok",
        }
    ]


@pytest.mark.parametrize(
    "exc_class",
    [IdentityResolutionError, AgentAuthError],
    ids=["resolution_error", "agent_auth_error"],
)
def test_list_identities_gh_token_login_blocked(
    tmp_path: Path,
    monkeypatch,
    exc_class: type[Exception],
) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_login", "newlogin")
    monkeypatch.setattr(
        "agentshore.sidecar.identities.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="resolved-token\n"
        ),
    )

    def _raise(_project_path: Path, _identity_env: dict[str, str]) -> None:
        raise exc_class("denied")

    monkeypatch.setattr(
        "agentshore.sidecar.identities.verify_identity_repo_access",
        _raise,
    )

    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "newlogin",
            "source": "gh_token_login",
            "token_status": "configured",
            "repo_access": "blocked",
        }
    ]


def test_list_identities_gh_token_keychain_missing_when_keyring_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """Keychain identity with no entry stored shows token_status=missing.

    Previously this returned token_status="unknown" because the sidecar
    didn't probe keychain at all. That made every keychain-backed
    identity look like a warning in the desktop UI even when correctly
    configured. The probe path mirrors the runtime resolver in
    agentshore.agents.identity.
    """
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_keychain", "agentshore:gh:newlogin")

    import keyring as _keyring

    monkeypatch.setattr(_keyring, "get_password", lambda _svc, _user: None)

    def _fail_if_called(_project_path: Path, _identity_env: dict[str, str]) -> None:
        raise AssertionError("verify_identity_repo_access should not be called without a token")

    monkeypatch.setattr(
        "agentshore.sidecar.identities.verify_identity_repo_access",
        _fail_if_called,
    )

    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "newlogin",
            "source": "gh_token_keychain",
            "token_status": "missing",
            "repo_access": "unknown",
        }
    ]


def test_list_identities_gh_token_keychain_configured_when_keyring_has_token(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_keychain", "agentshore:gh:newlogin")

    import keyring as _keyring

    monkeypatch.setattr(_keyring, "get_password", lambda _svc, _user: "fake-token-value")

    calls: list[dict[str, str]] = []

    def _record(_project_path: Path, identity_env: dict[str, str]) -> None:
        calls.append(identity_env)

    monkeypatch.setattr(
        "agentshore.sidecar.identities.verify_identity_repo_access",
        _record,
    )

    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "newlogin",
            "source": "gh_token_keychain",
            "token_status": "configured",
            "repo_access": "ok",
        }
    ]
    assert calls == [{"GH_TOKEN": "fake-token-value", "GITHUB_TOKEN": "fake-token-value"}]


def test_update_identity_rejects_unsupported_token_source(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)

    with pytest.raises(ValueError, match="unsupported token_source"):
        update_identity(tmp_path, "oldlogin", {"token_source": "bogus"})


def test_rpc_update_identity_unsupported_token_source_returns_invalid_params(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)
    monkeypatch.chdir(tmp_path)

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "identities.update",
            "params": {"login": "oldlogin", "patch": {"token_source": "bogus"}},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_remove_identity_rejects_missing_login(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)

    with pytest.raises(ValueError, match="identity not found"):
        remove_identity(tmp_path, "ghost")


def test_rpc_remove_identity_missing_login_returns_invalid_params(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)
    monkeypatch.chdir(tmp_path)

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "identities.remove",
            "params": {"login": "ghost"},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_add_identity_rejects_token_login_mismatch(tmp_path: Path, monkeypatch) -> None:
    """When the token belongs to a different GitHub user than the login
    provided, add_identity raises a clear error."""
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)

    monkeypatch.setattr(
        "agentshore.sidecar.identities.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="ghtoken123\n"
        ),
    )
    monkeypatch.setattr(
        "agentshore.sidecar.identities.resolve_github_login_for_token",
        lambda _token: "realUserName",
    )

    with pytest.raises(ValueError, match="token belongs to GitHub user 'realUserName'"):
        add_identity(tmp_path, "typoUserNam", "gh_token_login")


def test_add_identity_succeeds_when_token_matches(tmp_path: Path, monkeypatch) -> None:
    """add_identity writes to yaml when the token login matches."""
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)

    monkeypatch.setattr(
        "agentshore.sidecar.identities.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="ghtoken123\n"
        ),
    )
    monkeypatch.setattr(
        "agentshore.sidecar.identities.resolve_github_login_for_token",
        lambda _token: "CorrectUser",
    )

    add_identity(tmp_path, "CorrectUser", "gh_token_login")
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "correctuser" in data["identities"]


def test_add_identity_skips_validation_when_token_unresolvable(
    tmp_path: Path, monkeypatch
) -> None:
    """When the token can't be resolved (gh missing, env unset), skip
    validation and persist anyway — failure surfaces at session start."""
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)

    monkeypatch.setenv("MYUSER_GH_TOKEN", "some-token")
    monkeypatch.setattr(
        "agentshore.identity_names.resolve_github_login_for_token",
        lambda _token: None,
    )

    add_identity(tmp_path, "MyUser", "gh_token_env")
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "myuser" in data["identities"]
