from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
import yaml

from agentshore.errors import AgentAuthError
from agentshore.identity_names import keychain_service_for_login
from agentshore.sidecar.identities import (
    add_identity,
    check_identity_access,
    keychain_status,
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


def test_keychain_status_reports_existing_token(monkeypatch) -> None:
    import keyring as _keyring

    monkeypatch.setattr(_keyring, "get_password", lambda _svc, _user: "stored-pat-value")
    status = keychain_status("OctoCat")
    assert status == {
        "login": "octocat",
        "service": keychain_service_for_login("octocat"),
        "has_token": True,
    }


def test_keychain_status_reports_absent_token(monkeypatch) -> None:
    import keyring as _keyring

    monkeypatch.setattr(_keyring, "get_password", lambda _svc, _user: None)
    status = keychain_status("octocat")
    assert status["has_token"] is False
    assert status["login"] == "octocat"


def test_keychain_status_times_out_when_backend_hangs(monkeypatch) -> None:
    import keyring as _keyring

    def slow_get_password(_service: str, _username: str) -> str:
        time.sleep(0.05)
        return "late-token"

    monkeypatch.setattr(_keyring, "get_password", slow_get_password)
    monkeypatch.setattr("agentshore.sidecar.identities._KEYRING_TIMEOUT_SECONDS", 0.001)

    status = keychain_status("octocat")

    assert status["has_token"] is False
    time.sleep(0.06)


def test_keychain_status_rejects_invalid_login() -> None:
    with pytest.raises(ValueError, match="invalid.*login|login.*invalid"):
        keychain_status("has space")


def test_rpc_check_keychain_returns_status(monkeypatch) -> None:
    import keyring as _keyring

    monkeypatch.setattr(_keyring, "get_password", lambda _svc, _user: "stored-pat-value")
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "identities.check_keychain",
            "params": {"login": "octocat"},
        }
    )
    assert response is not None
    assert response["result"]["has_token"] is True
    assert response["result"]["login"] == "octocat"


def test_rpc_check_keychain_invalid_login_returns_invalid_params() -> None:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "identities.check_keychain",
            "params": {"login": "has space"},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_rpc_check_keychain_missing_login_returns_invalid_params() -> None:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "identities.check_keychain",
            "params": {},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_rpc_check_access_returns_identity_status(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_login", "newlogin")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "agentshore.sidecar.identities._token_for_identity",
        lambda _raw: ("fake-token-value", "gh_token_login"),
    )
    monkeypatch.setattr(
        "agentshore.sidecar.identities.verify_identity_repo_access",
        lambda _project_path, _identity_env: None,
    )

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "identities.check_access",
            "params": {"login": "newlogin"},
        }
    )

    assert response is not None
    assert response["result"]["repo_access"] == "ok"


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
            "token_status": "configured",
            "repo_access": "unknown",
        }
    ]


def test_identities_list_gh_login_does_not_resolve_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    def fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("identities.list should not call gh auth token")

    monkeypatch.setattr("agentshore.sidecar.identities.subprocess.run", fail_run)

    rows = list_identities(tmp_path)

    assert rows == [
        {
            "login": "oldlogin",
            "source": "gh_token_login",
            "token_status": "configured",
            "repo_access": "unknown",
        }
    ]


def test_identities_list_does_not_resolve_runtime_token(tmp_path: Path, monkeypatch) -> None:
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

    def fail_token_resolver(_raw: dict[str, object]) -> tuple[str | None, str]:
        raise AssertionError("identities.list should not resolve runtime tokens")

    monkeypatch.setattr("agentshore.sidecar.identities._token_for_identity", fail_token_resolver)

    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "oldlogin",
            "source": "gh_token_login",
            "token_status": "configured",
            "repo_access": "unknown",
        }
    ]


def test_list_identities_gh_token_login_happy_path(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_login", "newlogin")

    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "newlogin",
            "source": "gh_token_login",
            "token_status": "configured",
            "repo_access": "unknown",
        }
    ]


def test_list_identities_gh_token_env_configured_when_present(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_env", "NEWLOGIN_GH_TOKEN")
    monkeypatch.setenv("NEWLOGIN_GH_TOKEN", "fake-token-value")

    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "newlogin",
            "source": "gh_token_env",
            "token_status": "configured",
            "repo_access": "unknown",
        }
    ]


def test_list_identities_gh_token_keychain_configured_without_keyring_probe(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_keychain", "agentshore:gh:newlogin")

    import keyring as _keyring

    monkeypatch.setattr(_keyring, "get_password", lambda _svc, _user: None)

    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "newlogin",
            "source": "gh_token_keychain",
            "token_status": "configured",
            "repo_access": "unknown",
        }
    ]


def test_list_identities_gh_token_keychain_does_not_read_keyring(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_keychain", "agentshore:gh:newlogin")

    import keyring as _keyring

    def fail_get_password(_service: str, _username: str) -> str:
        raise AssertionError("identities.list should not read the OS credential store")

    monkeypatch.setattr(_keyring, "get_password", fail_get_password)

    rows = list_identities(tmp_path)
    assert rows == [
        {
            "login": "newlogin",
            "source": "gh_token_keychain",
            "token_status": "configured",
            "repo_access": "unknown",
        }
    ]


def test_check_identity_access_uses_resolved_token_for_repo_preflight(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_login", "newlogin")
    monkeypatch.setattr(
        "agentshore.sidecar.identities._token_for_identity",
        lambda _raw: ("fake-token-value", "gh_token_login"),
    )
    calls: list[dict[str, str]] = []

    def record_access(_project_path: Path, identity_env: dict[str, str]) -> None:
        calls.append(identity_env)

    monkeypatch.setattr(
        "agentshore.sidecar.identities.verify_identity_repo_access",
        record_access,
    )

    row = check_identity_access(tmp_path, "newlogin")

    assert row == {
        "login": "newlogin",
        "source": "gh_token_login",
        "token_status": "configured",
        "repo_access": "ok",
        "repo_access_detail": "GitHub repository access verified.",
    }
    assert calls == [{"GH_TOKEN": "fake-token-value", "GITHUB_TOKEN": "fake-token-value"}]


def test_check_identity_access_reports_blocked_repo_preflight(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_env", "NEWLOGIN_GH_TOKEN")
    monkeypatch.setenv("NEWLOGIN_GH_TOKEN", "fake-token-value")

    def raise_denied(_project_path: Path, _identity_env: dict[str, str]) -> None:
        raise AgentAuthError("denied")

    monkeypatch.setattr(
        "agentshore.sidecar.identities.verify_identity_repo_access",
        raise_denied,
    )

    row = check_identity_access(tmp_path, "newlogin")

    assert row == {
        "login": "newlogin",
        "source": "gh_token_env",
        "token_status": "configured",
        "repo_access": "blocked",
        "repo_access_detail": "denied",
    }


def test_check_identity_access_reports_missing_when_token_cannot_resolve(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_identity_config(cfg, "newlogin", "gh_token_keychain", "agentshore:gh:newlogin")
    monkeypatch.setattr(
        "agentshore.sidecar.identities._token_for_identity",
        lambda _raw: (None, "gh_token_keychain"),
    )

    row = check_identity_access(tmp_path, "newlogin")

    assert row == {
        "login": "newlogin",
        "source": "gh_token_keychain",
        "token_status": "missing",
        "repo_access": "unknown",
        "repo_access_detail": "Token could not be resolved from gh_token_keychain.",
    }


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


def test_add_identity_keychain_store_timeout_is_clear(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_minimal_config(cfg)

    import keyring as _keyring

    def slow_set_password(_service: str, _username: str, _password: str) -> None:
        time.sleep(0.05)

    monkeypatch.setattr(_keyring, "set_password", slow_set_password)
    monkeypatch.setattr("agentshore.sidecar.identities._KEYRING_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(ValueError, match="credential store did not respond in time"):
        add_identity(tmp_path, "NewLogin", "gh_token_keychain", pat="secret")

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "newlogin" not in data["identities"]
    time.sleep(0.06)


def test_add_identity_skips_validation_when_token_unresolvable(tmp_path: Path, monkeypatch) -> None:
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
