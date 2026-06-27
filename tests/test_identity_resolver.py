"""Tests for ``agentshore.agents.identity.resolve_identity_env``."""

from __future__ import annotations

import io
import json
import ssl
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from agentshore.agents import identity as identity_mod
from agentshore.agents.identity import reset_token_cache, resolve_identity_env
from agentshore.command import CommandResult, CommandStatus
from agentshore.config import AgentConfig, GitHubIdentity, RuntimeConfig
from agentshore.errors import AgentAuthError


def _cmd(
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
    tool_missing: bool = False,
) -> CommandResult:
    """Build a ``CommandResult`` mirroring what ``command.gh_sync``/``git_sync`` return."""
    if tool_missing:
        return CommandResult(
            args=("gh",),
            returncode=127,
            stdout="",
            stderr=stderr,
            status=CommandStatus.TOOL_NOT_FOUND,
        )
    status = CommandStatus.OK if returncode == 0 else CommandStatus.NONZERO
    return CommandResult(
        args=("gh",), returncode=returncode, stdout=stdout, stderr=stderr, status=status
    )


def _cfg(
    *,
    identity_name: str | None = "example-user",
    identities: dict[str, GitHubIdentity] | None = None,
) -> tuple[RuntimeConfig, AgentConfig]:
    if identities is None:
        identities = {
            "example-user": GitHubIdentity(
                git_user_name="Example User",
                git_user_email="user@example.com",
                gh_token_login="example-user",
            )
        }
    fc = RuntimeConfig(identities=identities)
    ac = AgentConfig(identity=identity_name)
    return fc, ac


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    reset_token_cache()


def test_no_identity_returns_empty() -> None:
    fc, ac = _cfg(identity_name=None)
    assert resolve_identity_env(fc, ac) == {}


def test_authorship_env_always_present() -> None:
    fc, ac = _cfg(
        identities={
            "example-user": GitHubIdentity(
                git_user_name="Example User",
                git_user_email="user@example.com",
            )
        }
    )
    env = resolve_identity_env(fc, ac)
    assert env["GIT_AUTHOR_NAME"] == "Example User"
    assert env["GIT_AUTHOR_EMAIL"] == "user@example.com"
    assert env["GIT_COMMITTER_NAME"] == "Example User"
    assert env["GIT_COMMITTER_EMAIL"] == "user@example.com"
    # No token configured -> nothing injected.
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


def test_token_env_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_USER_GH_TOKEN", "ghp_secret")
    fc, ac = _cfg(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot",
                git_user_email="bot@example.com",
                gh_token_env="BOT_USER_GH_TOKEN",
            )
        },
        identity_name="bot-user",
    )
    env = resolve_identity_env(fc, ac)
    assert env["GH_TOKEN"] == "ghp_secret"
    assert env["GITHUB_TOKEN"] == "ghp_secret"


def test_token_env_missing_logs_warning_and_omits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOT_USER_GH_TOKEN", raising=False)
    fc, ac = _cfg(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot",
                git_user_email="bot@example.com",
                gh_token_env="BOT_USER_GH_TOKEN",
            )
        },
        identity_name="bot-user",
    )
    env = resolve_identity_env(fc, ac)
    assert "GH_TOKEN" not in env


def test_strict_token_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentshore.agents.identity import IdentityResolutionError

    monkeypatch.delenv("BOT_USER_GH_TOKEN", raising=False)
    fc, ac = _cfg(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot",
                git_user_email="bot@example.com",
                gh_token_env="BOT_USER_GH_TOKEN",
            )
        },
        identity_name="bot-user",
    )
    with pytest.raises(IdentityResolutionError, match="token missing"):
        resolve_identity_env(fc, ac, strict=True)


def test_strict_token_env_rejects_wrong_login(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentshore.agents.identity import IdentityResolutionError

    monkeypatch.setenv("BOT_USER_GH_TOKEN", "ghp_wrong_login")
    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "validate_github_token",
        lambda _self, _token: (True, "someoneElse", None),
    )
    fc, ac = _cfg(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="Bot User",
                git_user_email="bot@example.com",
                gh_token_env="BOT_USER_GH_TOKEN",
            )
        },
        identity_name="bot-user",
    )

    with pytest.raises(IdentityResolutionError, match="expected 'bot-user'"):
        resolve_identity_env(fc, ac, strict=True)


def test_token_login_uses_gh_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_gh_sync(*args: str, **_: Any) -> CommandResult:
        calls.append(args)
        return _cmd("ghp_from_gh_cli\n")

    monkeypatch.setattr(identity_mod.command, "gh_sync", fake_gh_sync)

    fc, ac = _cfg()  # default uses gh_token_login="example-user"
    env = resolve_identity_env(fc, ac)

    assert env["GH_TOKEN"] == "ghp_from_gh_cli"
    assert calls == [("auth", "token", "-h", "github.com", "-u", "example-user")]


def test_token_login_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_gh_sync(*args: str, **_: Any) -> CommandResult:
        calls.append(args)
        return _cmd("ghp_cached\n")

    monkeypatch.setattr(identity_mod.command, "gh_sync", fake_gh_sync)

    fc, ac = _cfg()
    resolve_identity_env(fc, ac)
    resolve_identity_env(fc, ac)
    resolve_identity_env(fc, ac)

    assert len(calls) == 1


def test_token_login_uses_gh_config_dir_for_lookup(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | None] = []

    def fake_gh_sync(*args: str, **kwargs: Any) -> CommandResult:
        assert args[:2] == ("auth", "token")
        overlay = kwargs.get("env_overlay") or {}
        calls.append(overlay.get("GH_CONFIG_DIR"))
        return _cmd("ghp_from_config_dir\n")

    monkeypatch.setattr(identity_mod.command, "gh_sync", fake_gh_sync)

    config_dir = tmp_path / "gh-one"
    fc, ac = _cfg(
        identities={
            "example-user": GitHubIdentity(
                git_user_name="Wes",
                git_user_email="user@example.com",
                gh_token_login="example-user",
                gh_config_dir=str(config_dir),
            )
        }
    )

    env = resolve_identity_env(fc, ac)

    assert env["GH_TOKEN"] == "ghp_from_config_dir"
    assert env["GH_CONFIG_DIR"] == str(config_dir)
    assert calls == [str(config_dir)]


def test_token_login_cache_is_scoped_by_gh_config_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | None] = []

    def fake_gh_sync(*args: str, **kwargs: Any) -> CommandResult:
        assert args[:2] == ("auth", "token")
        config_dir = (kwargs.get("env_overlay") or {}).get("GH_CONFIG_DIR")
        calls.append(config_dir)
        return _cmd(f"token_for_{config_dir}\n")

    monkeypatch.setattr(identity_mod.command, "gh_sync", fake_gh_sync)

    one = str(tmp_path / "one")
    two = str(tmp_path / "two")
    cfg = RuntimeConfig(
        identities={
            "first": GitHubIdentity(
                git_user_name="First",
                git_user_email="first@example.com",
                gh_token_login="shared",
                gh_config_dir=one,
            ),
            "second": GitHubIdentity(
                git_user_name="Second",
                git_user_email="second@example.com",
                gh_token_login="shared",
                gh_config_dir=two,
            ),
        }
    )

    first_env = resolve_identity_env(cfg, AgentConfig(identity="first"))
    second_env = resolve_identity_env(cfg, AgentConfig(identity="second"))

    assert first_env["GH_TOKEN"] == f"token_for_{one}"
    assert second_env["GH_TOKEN"] == f"token_for_{two}"
    assert calls == [one, two]


def test_token_login_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh_sync(*_args: str, **_: Any) -> CommandResult:
        return _cmd(returncode=1, stderr="not logged in")

    monkeypatch.setattr(identity_mod.command, "gh_sync", fake_gh_sync)

    fc, ac = _cfg()
    env = resolve_identity_env(fc, ac)
    assert "GH_TOKEN" not in env
    # Authorship still set.
    assert env["GIT_AUTHOR_NAME"] == "Example User"


def test_gh_cli_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity_mod.command, "gh_sync", lambda *_a, **_k: _cmd(tool_missing=True))
    fc, ac = _cfg()
    env = resolve_identity_env(fc, ac)
    assert "GH_TOKEN" not in env


def test_ssh_key_path_emits_git_ssh_command() -> None:
    fc, ac = _cfg(
        identities={
            "example-user": GitHubIdentity(
                git_user_name="Wes",
                git_user_email="user@example.com",
                ssh_key_path="/keys/id_ed25519",
            )
        }
    )
    env = resolve_identity_env(fc, ac)
    assert env["GIT_SSH_COMMAND"] == ("ssh -i /keys/id_ed25519 -o IdentitiesOnly=yes")


def test_unknown_identity_at_dispatch_returns_empty_authorship() -> None:
    """If parser-level validation is bypassed, the resolver fails open."""
    fc = RuntimeConfig(identities={})
    ac = AgentConfig(identity="ghost")
    assert resolve_identity_env(fc, ac) == {}


def test_isolated_gh_config_dir_when_unspecified(tmp_path, monkeypatch) -> None:
    """Identity without an explicit gh_config_dir gets an isolated empty one.

    Regression for #316: a codex/claude subprocess that scrubs GH_TOKEN
    from its env will fall back to gh's hosts.yml. If we leave the parent's
    GH_CONFIG_DIR in place that means falling back to the user's default
    account — silent identity impersonation. The fix forces gh to either use
    the injected GH_TOKEN or fail explicitly: never silently impersonate.
    """
    import agentshore.agents.identity as ident_mod

    monkeypatch.setattr(ident_mod, "user_data_dir", lambda _name: str(tmp_path), raising=False)
    fc, ac = _cfg(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot",
                git_user_email="bot@example.com",
            )
        },
        identity_name="bot-user",
    )
    env = resolve_identity_env(fc, ac)
    gh_dir = Path(env["GH_CONFIG_DIR"])
    assert gh_dir.is_dir(), "isolated GH_CONFIG_DIR must exist"
    assert not any(gh_dir.iterdir()), "isolated dir must be empty so gh has no fallback"
    assert gh_dir.name == "gh"
    assert "bot-user" in gh_dir.parts


def test_explicit_gh_config_dir_takes_precedence(tmp_path) -> None:
    """A configured gh_config_dir wins over the auto-isolated path."""
    explicit = tmp_path / "configured"
    explicit.mkdir()
    fc, ac = _cfg(
        identities={
            "example-user": GitHubIdentity(
                git_user_name="Wes",
                git_user_email="user@example.com",
                gh_config_dir=str(explicit),
            )
        }
    )
    env = resolve_identity_env(fc, ac)
    assert env["GH_CONFIG_DIR"] == str(explicit)


# report_identities — diagnostic / startup banner


def _report(*, identities: dict[str, GitHubIdentity], agents: dict[str, AgentConfig]) -> list:
    from agentshore.agents.identity import report_identities

    fc = RuntimeConfig(identities=identities, agents=agents)
    return report_identities(fc)


def test_report_no_identity_marks_ambient() -> None:
    rows = _report(
        identities={},
        agents={"claude_code": AgentConfig(identity=None)},
    )
    (row,) = rows
    assert row.identity_name is None
    assert row.token_resolved is False
    assert row.token_source == "none"


def test_report_env_token_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_USER_GH_TOKEN", "ghp_x")
    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "validate_github_token",
        lambda _self, _token: (True, "bot-user", None),
    )
    rows = _report(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot",
                git_user_email="bot@example.com",
                gh_token_env="BOT_USER_GH_TOKEN",
            )
        },
        agents={"codex": AgentConfig(identity="bot-user")},
    )
    (row,) = rows
    assert row.token_source == "env"
    assert row.token_resolved is True
    assert row.token_valid is True
    assert row.resolved_login == "bot-user"
    assert "BOT_USER_GH_TOKEN" in row.detail


def test_report_env_token_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_USER_GH_TOKEN", "ghp_bad")
    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "validate_github_token",
        lambda _self, _token: (False, None, "HTTP 401 Bad credentials"),
    )
    rows = _report(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot",
                git_user_email="bot@example.com",
                gh_token_env="BOT_USER_GH_TOKEN",
            )
        },
        agents={"codex": AgentConfig(identity="bot-user")},
    )
    (row,) = rows
    assert row.token_resolved is True
    assert row.token_valid is False
    assert row.resolved_login is None
    assert "Bad credentials" in row.detail


def test_report_env_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOT_USER_GH_TOKEN", raising=False)
    rows = _report(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot",
                git_user_email="bot@example.com",
                gh_token_env="BOT_USER_GH_TOKEN",
            )
        },
        agents={"codex": AgentConfig(identity="bot-user")},
    )
    (row,) = rows
    assert row.token_resolved is False
    assert "is unset" in row.detail
    assert "BOT_USER_GH_TOKEN" in row.detail


def test_keychain_token_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentshore import keyring_child

    monkeypatch.setattr(
        keyring_child,
        "keyring_get",
        lambda service: "ghp_keychain_token" if service == "agentshore/bot-user" else None,
    )

    fc = RuntimeConfig(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot",
                git_user_email="bot@example.com",
                gh_token_keychain="agentshore/bot-user",
            )
        }
    )
    ac = AgentConfig(identity="bot-user")
    env = resolve_identity_env(fc, ac)
    assert env["GH_TOKEN"] == "ghp_keychain_token"


def test_keychain_lowercase_service_fallback_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentshore import keyring_child

    monkeypatch.setattr(
        keyring_child,
        "keyring_get",
        lambda service: "ghp_keychain_token" if service == "agentshore/bot-user" else None,
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        identity_mod._logger,
        "warning",
        lambda event, **_fields: warnings.append(str(event)),
    )
    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "validate_github_token",
        lambda _self, _token: (True, "bot-user", None),
    )

    fc = RuntimeConfig(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot",
                git_user_email="bot@example.com",
                gh_token_keychain="agentshore/bot-user",
            )
        }
    )
    ac = AgentConfig(identity="bot-user")
    env = resolve_identity_env(fc, ac, strict=True)
    assert env["GH_TOKEN"] == "ghp_keychain_token"
    assert "identity_keychain_token_empty" not in warnings


def test_repo_scoped_keychain_service_validates_login_from_last_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "read_keychain_token",
        lambda _self, service, warn_missing=True: (
            "ghp_keychain_token"
            if service == "agentshore/example-user/example-repo/bot-user"
            else None
        ),
    )
    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "validate_github_token",
        lambda _self, _token: (True, "bot-user", None),
    )

    fc = RuntimeConfig(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot",
                git_user_email="bot@example.com",
                gh_token_keychain="agentshore/example-user/example-repo/bot-user",
            )
        }
    )
    ac = AgentConfig(identity="bot-user")

    env = resolve_identity_env(fc, ac, strict=True)

    assert env["GH_TOKEN"] == "ghp_keychain_token"


def test_strict_keychain_token_rejects_wrong_agentshore_service_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentshore.agents.identity import IdentityResolutionError

    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "read_keychain_token",
        lambda _self, service, warn_missing=True: (
            "ghp_keychain_token" if service == "agentshore/bot-user" else None
        ),
    )
    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "validate_github_token",
        lambda _self, _token: (True, "someoneElse", None),
    )

    fc = RuntimeConfig(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="Bot User",
                git_user_email="bot@example.com",
                gh_token_keychain="agentshore/bot-user",
            )
        }
    )

    with pytest.raises(IdentityResolutionError, match="expected 'bot-user'"):
        resolve_identity_env(fc, AgentConfig(identity="bot-user"), strict=True)


def test_strict_custom_keychain_token_requires_configured_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentshore.agents.identity import IdentityResolutionError

    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "read_keychain_token",
        lambda _self, service, warn_missing=True: (
            "ghp_custom" if service == "custom/service" else None
        ),
    )
    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "validate_github_token",
        lambda _self, _token: (True, "bot-user", None),
    )

    fc = RuntimeConfig(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="Bot User",
                git_user_email="bot@example.com",
                gh_token_keychain="custom/service",
            )
        }
    )

    with pytest.raises(IdentityResolutionError, match="no configured GitHub login"):
        resolve_identity_env(fc, AgentConfig(identity="bot-user"), strict=True)


def test_keychain_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *_a, **_kw: None)
    fc = RuntimeConfig(
        identities={
            "x": GitHubIdentity(
                git_user_name="x",
                git_user_email="x@example.com",
                gh_token_keychain="agentshore/x",
            )
        }
    )
    ac = AgentConfig(identity="x")
    env = resolve_identity_env(fc, ac)
    assert "GH_TOKEN" not in env


def test_keychain_backend_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    from keyring.errors import KeyringError

    def _boom(*_a: object, **_kw: object) -> str | None:
        raise KeyringError("backend unavailable")

    monkeypatch.setattr(keyring, "get_password", _boom)
    fc = RuntimeConfig(
        identities={
            "x": GitHubIdentity(
                git_user_name="x",
                git_user_email="x@example.com",
                gh_token_keychain="agentshore/x",
            )
        }
    )
    ac = AgentConfig(identity="x")
    env = resolve_identity_env(fc, ac)
    assert "GH_TOKEN" not in env
    # Authorship still set even when the backend errored.
    assert env["GIT_AUTHOR_NAME"] == "x"


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/Owner/Repo.git", "Owner/Repo"),
        ("git@github.com:Owner/Repo.git", "Owner/Repo"),
        ("ssh://git@github.com/Owner/Repo.git", "Owner/Repo"),
    ],
)
def test_parse_github_remote_name_supports_common_formats(remote: str, expected: str) -> None:
    assert identity_mod._parse_github_remote_name(remote) == expected


def test_verify_repo_access_uses_github_rest_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, str | None] = {}

    def fake_git_sync(*args: str, **_: Any) -> CommandResult:
        assert args == ("config", "--get", "remote.origin.url")
        return _cmd("https://github.com/Owner/Repo.git\n")

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"full_name": "Owner/Repo", "permissions": {"push": True}}).encode(
                "utf-8"
            )

    def fake_urlopen(request: Any, **kwargs: Any) -> Response:
        calls["url"] = request.full_url
        calls["authorization"] = request.get_header("Authorization")
        calls["user_agent"] = request.get_header("User-agent")
        calls["context"] = kwargs.get("context")
        return Response()

    monkeypatch.setattr(identity_mod.command, "git_sync", fake_git_sync)
    monkeypatch.setattr(identity_mod.urllib.request, "urlopen", fake_urlopen)

    identity_mod.verify_identity_repo_access(tmp_path, {"GH_TOKEN": "token-secret"})

    # A CA-backed SSL context must be supplied: the managed sidecar venv's Python
    # ships no CA bundle, so urllib's default context fails with
    # CERTIFICATE_VERIFY_FAILED. Regression guard for that bug.
    context = calls.pop("context")
    assert isinstance(context, ssl.SSLContext)
    assert context.get_ca_certs(), "preflight SSL context has no CA certificates"
    assert calls == {
        "url": "https://api.github.com/repos/Owner/Repo",
        "authorization": "Bearer token-secret",
        "user_agent": "AgentShore",
    }


def test_github_api_ssl_context_is_ca_backed() -> None:
    # On macOS/Linux the preflight context must come from certifi (the managed
    # venv Python has no usable system CA store); assert it has CAs loaded.
    context = identity_mod._github_api_ssl_context()
    assert isinstance(context, ssl.SSLContext)
    assert context.get_ca_certs()


def test_verify_repo_access_reports_github_api_denial_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_git_sync(*_args: str, **_: Any) -> CommandResult:
        return _cmd("git@github.com:Owner/Repo.git\n")

    def fake_urlopen(_request: Any, **_: Any) -> None:
        raise urllib.error.HTTPError(
            url="https://api.github.com/repos/Owner/Repo",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=io.BytesIO(b'{"message":"Not Found"}'),
        )

    monkeypatch.setattr(identity_mod.command, "git_sync", fake_git_sync)
    monkeypatch.setattr(identity_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(AgentAuthError) as exc_info:
        identity_mod.verify_identity_repo_access(tmp_path, {"GH_TOKEN": "token-secret"})

    detail = str(exc_info.value)
    assert "Owner/Repo returned HTTP 404: Not Found" in detail
    assert "token-secret" not in detail


def test_report_identity_repo_access_flags_wrong_repo_pat(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid token scoped away from the repo is a startup-blocking config issue."""

    monkeypatch.setenv("BOT_GH_TOKEN", "ghp_valid_for_other_repo")
    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "validate_github_token",
        lambda _self, _token: (True, "bot-user", None),
    )

    def deny_repo_access(_project_path, _identity_env):
        raise AgentAuthError(
            "GitHub repository access preflight failed for the assigned identity token: "
            "GraphQL: Could not resolve to a Repository with the name 'example-user/example-repo'."
        )

    monkeypatch.setattr(identity_mod, "verify_identity_repo_access", deny_repo_access)
    cfg = RuntimeConfig(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot-user",
                git_user_email="bot@example.com",
                gh_token_env="BOT_GH_TOKEN",
            ),
        },
        agents={"codex": AgentConfig(identity="bot-user")},
    )

    rows = identity_mod.report_identity_repo_access(cfg, tmp_path)

    assert len(rows) == 1
    assert rows[0].agent_key == "codex"
    assert rows[0].identity_name == "bot-user"
    assert rows[0].ok is False
    assert "Could not resolve" in rows[0].detail


def test_report_identity_repo_access_skips_disabled_agents(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_GH_TOKEN", "ghp_valid")
    monkeypatch.setattr(
        identity_mod.IdentityResolver,
        "validate_github_token",
        lambda _self, _token: (True, "bot-user", None),
    )
    checked = False

    def record_repo_access(_project_path, _identity_env):
        nonlocal checked
        checked = True

    monkeypatch.setattr(identity_mod, "verify_identity_repo_access", record_repo_access)
    cfg = RuntimeConfig(
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="bot-user",
                git_user_email="bot@example.com",
                gh_token_env="BOT_GH_TOKEN",
            ),
        },
        agents={"codex": AgentConfig(enabled=False, identity="bot-user")},
    )

    assert identity_mod.report_identity_repo_access(cfg, tmp_path) == []
    assert checked is False


def test_three_token_sources_rejected_at_parse(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from agentshore.config import load_config
    from agentshore.errors import ConfigError

    yaml_text = """\
agents:
  claude_code:
    enabled: true
identities:
  bad:
    git_user_name: x
    git_user_email: x@example.com
    gh_token_env: X_TOKEN
    gh_token_keychain: x/k
"""
    p = tmp_path / "agentshore.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="at most one of"):
        load_config(p)


def test_report_gh_login_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh_sync(*args: str, **_: Any) -> CommandResult:
        if args[:2] == ("auth", "token"):
            return _cmd("ghp_login_token\n")
        if args[:2] == ("api", "user"):
            return _cmd("example-user\n")
        raise AssertionError(args)

    monkeypatch.setattr(identity_mod.command, "gh_sync", fake_gh_sync)

    rows = _report(
        identities={
            "example-user": GitHubIdentity(
                git_user_name="Wes",
                git_user_email="user@example.com",
                gh_token_login="example-user",
            )
        },
        agents={"claude_code": AgentConfig(identity="example-user")},
    )
    (row,) = rows
    assert row.token_source == "gh_login"
    assert row.token_resolved is True
    assert row.token_valid is True
    assert row.resolved_login == "example-user"


# require_two_distinct_gh_identities


def test_require_two_identities_passes_with_two_logins() -> None:
    from agentshore.agents.identity import require_two_distinct_gh_identities

    fc = RuntimeConfig(
        identities={
            "alice": GitHubIdentity(
                git_user_name="Alice",
                git_user_email="alice@example.com",
                gh_token_login="alice",
            ),
            "bob": GitHubIdentity(
                git_user_name="Bob",
                git_user_email="bob@example.com",
                gh_token_login="bob",
            ),
        },
        agents={
            "claude_code": AgentConfig(identity="alice"),
            "codex": AgentConfig(identity="bob"),
        },
    )
    require_two_distinct_gh_identities(fc)  # no raise


def test_require_two_identities_raises_when_all_share_one_login() -> None:
    from agentshore.agents.identity import require_two_distinct_gh_identities
    from agentshore.errors import ConfigError

    fc = RuntimeConfig(
        identities={
            "alice": GitHubIdentity(
                git_user_name="Alice",
                git_user_email="alice@example.com",
                gh_token_login="alice",
            ),
        },
        agents={
            "claude_code": AgentConfig(identity="alice"),
            "codex": AgentConfig(identity="alice"),
        },
    )
    with pytest.raises(ConfigError, match=r"≥2 distinct GitHub identities"):
        require_two_distinct_gh_identities(fc)


def test_require_two_identities_raises_when_no_identity_configured() -> None:
    from agentshore.agents.identity import require_two_distinct_gh_identities
    from agentshore.errors import ConfigError

    fc = RuntimeConfig(
        agents={
            "claude_code": AgentConfig(),
            "codex": AgentConfig(),
        },
    )
    with pytest.raises(ConfigError, match=r"no identity bound"):
        require_two_distinct_gh_identities(fc)


def test_require_two_identities_no_op_when_no_cli_agents() -> None:
    """No enabled CLI agents → code_review is structurally unavailable, skip check."""
    from agentshore.agents.identity import require_two_distinct_gh_identities

    require_two_distinct_gh_identities(RuntimeConfig())  # no raise


def test_require_two_identities_skips_disabled_agents() -> None:
    """Disabled agents don't count toward the identity diversity requirement."""
    from agentshore.agents.identity import require_two_distinct_gh_identities
    from agentshore.errors import ConfigError

    fc = RuntimeConfig(
        identities={
            "alice": GitHubIdentity(
                git_user_name="Alice",
                git_user_email="alice@example.com",
                gh_token_login="alice",
            ),
            "bob": GitHubIdentity(
                git_user_name="Bob",
                git_user_email="bob@example.com",
                gh_token_login="bob",
            ),
        },
        agents={
            "claude_code": AgentConfig(identity="alice", enabled=True),
            "codex": AgentConfig(identity="bob", enabled=False),
        },
    )
    with pytest.raises(ConfigError, match=r"only one identity"):
        require_two_distinct_gh_identities(fc)


def test_require_two_identities_rejects_missing_token_source() -> None:
    """Identities with no token source are rejected (would inherit ambient gh auth)."""
    from agentshore.agents.identity import require_two_distinct_gh_identities
    from agentshore.errors import ConfigError

    fc = RuntimeConfig(
        identities={
            "alice": GitHubIdentity(
                git_user_name="Alice",
                git_user_email="alice@example.com",
                # no token source at all → inherits ambient gh auth
            ),
            "bob": GitHubIdentity(
                git_user_name="Bob",
                git_user_email="bob@example.com",
                # no token source at all → inherits ambient gh auth (likely SAME login)
            ),
        },
        agents={
            "claude_code": AgentConfig(identity="alice"),
            "codex": AgentConfig(identity="bob"),
        },
    )
    with pytest.raises(ConfigError, match=r"no token source"):
        require_two_distinct_gh_identities(fc)


def test_require_two_identities_accepts_keychain_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """AgentShore-managed gh_token_keychain services encode the login."""
    from agentshore.agents.identity import IdentityResolver, require_two_distinct_gh_identities

    # Pin the external resolution surfaces so the check is hermetic under xdist:
    # without this the resolver shells out to the OS keychain (keyring) and `gh`,
    # whose timing under parallel load made this flaky (#13). With both inert the
    # resolver falls back to config-derived logins — exactly the path under test
    # (a gh_token_keychain service name encodes its login).
    monkeypatch.setattr(identity_mod.command, "gh_sync", lambda *_a, **_k: _cmd(tool_missing=True))
    monkeypatch.setattr(IdentityResolver, "read_keychain_token", lambda self, service: None)

    fc = RuntimeConfig(
        identities={
            "example-user": GitHubIdentity(
                git_user_name="Wes",
                git_user_email="user@example.com",
                gh_token_login="example-user",
            ),
            "bot-user": GitHubIdentity(
                git_user_name="Bot User",
                git_user_email="bot@example.com",
                gh_token_keychain="agentshore/bot-user",
            ),
        },
        agents={
            "claude_code": AgentConfig(identity="example-user"),
            "codex": AgentConfig(identity="bot-user"),
        },
    )
    require_two_distinct_gh_identities(fc)  # no raise


def test_require_two_identities_accepts_env_token() -> None:
    """Env-token identities use the identity key as the login."""
    from agentshore.agents.identity import require_two_distinct_gh_identities

    fc = RuntimeConfig(
        identities={
            "alice": GitHubIdentity(
                git_user_name="Alice",
                git_user_email="alice@example.com",
                gh_token_env="ALICE_GH_TOKEN",
            ),
            "bob": GitHubIdentity(
                git_user_name="Bob",
                git_user_email="bob@example.com",
                gh_token_login="bob",
            ),
        },
        agents={
            "claude_code": AgentConfig(identity="alice"),
            "codex": AgentConfig(identity="bob"),
        },
    )
    require_two_distinct_gh_identities(fc)  # no raise


def test_require_two_identities_rejects_undefined_identity() -> None:
    """An agent referencing an identity name not in the identities block."""
    from agentshore.agents.identity import require_two_distinct_gh_identities
    from agentshore.errors import ConfigError

    fc = RuntimeConfig(
        identities={
            "alice": GitHubIdentity(
                git_user_name="Alice",
                git_user_email="alice@example.com",
                gh_token_login="alice",
            ),
        },
        agents={
            "claude_code": AgentConfig(identity="alice"),
            "codex": AgentConfig(identity="ghost"),
        },
    )
    with pytest.raises(ConfigError, match=r"not defined"):
        require_two_distinct_gh_identities(fc)


def test_verify_repo_access_requires_push_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response with push=False must raise AgentAuthError."""

    def fake_git_sync(*args: str, **_: Any) -> CommandResult:
        assert args == ("config", "--get", "remote.origin.url")
        return _cmd("https://github.com/Owner/Repo.git\n")

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"full_name": "Owner/Repo", "permissions": {"push": False, "pull": True}}
            ).encode("utf-8")

    def fake_urlopen(_request: Any, **_: Any) -> Response:
        return Response()

    monkeypatch.setattr(identity_mod.command, "git_sync", fake_git_sync)
    monkeypatch.setattr(identity_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(AgentAuthError) as exc_info:
        identity_mod.verify_identity_repo_access(tmp_path, {"GH_TOKEN": "token-secret"})

    detail = str(exc_info.value)
    assert "push" in detail or "write" in detail
    assert "Owner/Repo" in detail
    assert "token-secret" not in detail


def test_verify_repo_access_requires_push_permission_missing_permissions_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response with no permissions key must raise AgentAuthError."""

    def fake_git_sync(*args: str, **_: Any) -> CommandResult:
        assert args == ("config", "--get", "remote.origin.url")
        return _cmd("https://github.com/Owner/Repo.git\n")

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"full_name": "Owner/Repo"}).encode("utf-8")

    def fake_urlopen(_request: Any, **_: Any) -> Response:
        return Response()

    monkeypatch.setattr(identity_mod.command, "git_sync", fake_git_sync)
    monkeypatch.setattr(identity_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(AgentAuthError) as exc_info:
        identity_mod.verify_identity_repo_access(tmp_path, {"GH_TOKEN": "token-secret"})

    detail = str(exc_info.value)
    assert "push" in detail or "write" in detail
    assert "Owner/Repo" in detail
    assert "token-secret" not in detail
