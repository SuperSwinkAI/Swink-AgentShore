"""Multi-identity git-auth: token→HTTPS-header overlay, fetch-identity selection.

Covers the credential path that fixes the codex 3600s git-credential hang (#177)
without violating the per-agent identity invariant: each agent authenticates as
its *own* identity (token-derived header), and the shared worktree fetch uses a
single read-capable default identity (gh-OAuth-preferred).
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agentshore import subprocess_env
from agentshore.agents import identity as identity_mod
from agentshore.agents.worktree import manager as mgr
from agentshore.config import GitHubIdentity, RuntimeConfig
from agentshore.core import branch_sync


def _b64(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")


# --- subprocess_env.git_auth_config_overlay ---------------------------------


def test_overlay_injects_basic_auth_header_for_github() -> None:
    overlay = subprocess_env.git_auth_config_overlay("secret-token")
    count = int(overlay["GIT_CONFIG_COUNT"])
    pairs = {overlay[f"GIT_CONFIG_KEY_{i}"]: overlay[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert pairs["credential.helper"] == ""
    assert pairs["credential.interactive"] == "never"
    assert (
        pairs["http.https://github.com/.extraheader"]
        == f"Authorization: Basic {_b64('secret-token')}"
    )


def test_overlay_scopes_header_to_host() -> None:
    overlay = subprocess_env.git_auth_config_overlay("t", host="ghe.example.com")
    count = int(overlay["GIT_CONFIG_COUNT"])
    keys = [overlay[f"GIT_CONFIG_KEY_{i}"] for i in range(count)]
    assert "http.https://ghe.example.com/.extraheader" in keys
    assert all("github.com" not in k for k in keys)


def test_overlay_never_places_token_in_a_key() -> None:
    # Call sites log env KEYS only — the token must live only in a VALUE field.
    overlay = subprocess_env.git_auth_config_overlay("supersecret")
    assert all("supersecret" not in key for key in overlay)


# --- identity selection / resolution ----------------------------------------


def _ident(
    *, login: str | None = None, env: str | None = None, keychain: str | None = None
) -> GitHubIdentity:
    return GitHubIdentity(
        git_user_name="N",
        git_user_email="n@example.com",
        gh_token_login=login,
        gh_token_env=env,
        gh_token_keychain=keychain,
    )


def test_select_default_git_identity_prefers_gh_oauth_over_pat() -> None:
    cfg = RuntimeConfig(
        identities={"pat": _ident(env="PAT_ENV"), "oauth": _ident(login="oauth")},
    )
    assert identity_mod.select_default_git_identity(cfg) == "oauth"


def test_select_default_git_identity_falls_back_keychain_then_pat() -> None:
    cfg = RuntimeConfig(identities={"pat": _ident(env="PAT_ENV"), "kc": _ident(keychain="svc")})
    assert identity_mod.select_default_git_identity(cfg) == "kc"


def test_select_default_git_identity_same_rank_is_deterministic_by_name() -> None:
    cfg = RuntimeConfig(identities={"b": _ident(env="B_ENV"), "a": _ident(env="A_ENV")})
    assert identity_mod.select_default_git_identity(cfg) == "a"


def test_select_default_git_identity_none_without_identities() -> None:
    assert identity_mod.select_default_git_identity(RuntimeConfig(identities={})) is None


def test_resolve_identity_env_by_name_returns_authorship_overlay() -> None:
    cfg = RuntimeConfig(identities={"u": _ident(env="MISSING_ENV_VAR")})
    env = identity_mod.resolve_identity_env_by_name(cfg, "u")
    assert env["GIT_AUTHOR_NAME"] == "N"
    assert env["GIT_AUTHOR_EMAIL"] == "n@example.com"
    assert "GH_CONFIG_DIR" in env


def test_resolve_identity_env_by_name_missing_is_empty_nonstrict() -> None:
    assert identity_mod.resolve_identity_env_by_name(RuntimeConfig(identities={}), "nope") == {}


# --- manager fetch-overlay composition --------------------------------------


def test_resolve_fetch_overlay_none_without_identities() -> None:
    assert mgr._resolve_fetch_overlay(RuntimeConfig(identities={})) is None


def test_resolve_fetch_overlay_builds_header_and_preserves_identity_env(monkeypatch) -> None:
    cfg = RuntimeConfig(identities={"u": _ident(login="u")})
    monkeypatch.setattr(
        mgr,
        "resolve_identity_env_by_name",
        lambda _cfg, _name: {"GH_TOKEN": "tok", "GIT_AUTHOR_NAME": "U", "GH_CONFIG_DIR": "/cfg"},
    )
    overlay = mgr._resolve_fetch_overlay(cfg)
    assert overlay is not None
    values = [v for k, v in overlay.items() if k.startswith("GIT_CONFIG_VALUE_")]
    assert f"Authorization: Basic {_b64('tok')}" in values
    # the chosen identity's own env (authorship, gh config) is preserved
    assert overlay["GIT_AUTHOR_NAME"] == "U"
    assert overlay["GH_CONFIG_DIR"] == "/cfg"


def test_resolve_fetch_overlay_none_when_identity_has_no_token(monkeypatch) -> None:
    cfg = RuntimeConfig(identities={"u": _ident(login="u")})
    monkeypatch.setattr(
        mgr, "resolve_identity_env_by_name", lambda _cfg, _name: {"GIT_AUTHOR_NAME": "U"}
    )
    assert mgr._resolve_fetch_overlay(cfg) is None


# --- branch_sync FF-fetch overlay (#178) ------------------------------------


def test_resolve_ff_fetch_overlay_none_without_identities() -> None:
    assert branch_sync.resolve_ff_fetch_overlay(RuntimeConfig(identities={})) is None


def test_resolve_ff_fetch_overlay_builds_header_and_preserves_identity_env(monkeypatch) -> None:
    cfg = RuntimeConfig(identities={"u": _ident(login="u")})
    # resolve_ff_fetch_overlay lazy-imports from agentshore.agents.identity, so
    # patch the source module attribute (re-read on each call).
    monkeypatch.setattr(
        identity_mod,
        "resolve_identity_env_by_name",
        lambda _cfg, _name: {"GH_TOKEN": "tok", "GIT_AUTHOR_NAME": "U", "GH_CONFIG_DIR": "/cfg"},
    )
    overlay = branch_sync.resolve_ff_fetch_overlay(cfg)
    assert overlay is not None
    values = [v for k, v in overlay.items() if k.startswith("GIT_CONFIG_VALUE_")]
    assert f"Authorization: Basic {_b64('tok')}" in values
    assert overlay["GIT_AUTHOR_NAME"] == "U"
    assert overlay["GH_CONFIG_DIR"] == "/cfg"


def test_resolve_ff_fetch_overlay_none_when_identity_has_no_token(monkeypatch) -> None:
    cfg = RuntimeConfig(identities={"u": _ident(login="u")})
    monkeypatch.setattr(
        identity_mod, "resolve_identity_env_by_name", lambda _cfg, _name: {"GIT_AUTHOR_NAME": "U"}
    )
    assert branch_sync.resolve_ff_fetch_overlay(cfg) is None


@pytest.mark.asyncio
async def test_fast_forward_threads_fetch_overlay_to_git(monkeypatch) -> None:
    """The resolved auth overlay reaches the (and only the) remote fetch."""
    captured: dict[str, object] = {}

    async def fake_git(*args: str, cwd, timeout=120.0, env_overlay=None):  # type: ignore[no-untyped-def]
        captured["args"] = args
        captured["overlay"] = env_overlay
        # Non-zero rc → impl returns FETCH_FAILED after the fetch, so the fetch
        # is the only git call and we can assert its overlay in isolation.
        return (1, "", "fatal: unable to get password from user")

    monkeypatch.setattr(branch_sync, "_git", fake_git)
    overlay = {"GIT_CONFIG_COUNT": "3"}
    result = await branch_sync.fast_forward_local_branch(
        Path("/repo"), "main", fetch_env_overlay=overlay
    )
    assert result.status is branch_sync.FFSyncStatus.FETCH_FAILED
    assert captured["args"][0] == "fetch"
    assert captured["overlay"] == overlay


# --- worktree allocator ls-remote / fetch overlay (#179 + #151 audit) -------


@pytest.mark.asyncio
async def test_remote_branch_exists_threads_overlay_to_ls_remote(monkeypatch) -> None:
    """The branch-existence probe must carry the fetch identity's auth (#179).

    Without it, the hardened git layer (no credential helper) makes the
    ``ls-remote`` authenticate as nobody on a private HTTPS remote → empty →
    live branch misread as gone.
    """
    from agentshore.agents.worktree import allocator as alloc

    captured: dict[str, object] = {}

    async def fake_run_git(*args, cwd, check=True, timeout=60.0, env_overlay=None):  # type: ignore[no-untyped-def]
        captured["args"] = args
        captured["overlay"] = env_overlay
        return (0, "deadbeef\trefs/heads/feature", "")

    monkeypatch.setattr(alloc, "_run_git", fake_run_git)
    overlay = {"GIT_CONFIG_COUNT": "3"}
    ok = await alloc._remote_branch_exists(Path("/repo"), "feature", env_overlay=overlay)
    assert ok is True
    assert captured["args"][0] == "ls-remote"
    assert captured["overlay"] == overlay


@pytest.mark.asyncio
async def test_ensure_worktree_passes_fetch_overlay_to_branch_check(monkeypatch, tmp_path) -> None:
    """``ensure_worktree`` forwards ``fetch_env_overlay`` into the branch probe."""
    from agentshore.agents.worktree import allocator as alloc

    captured: dict[str, object] = {}

    async def fake_fetch(main_repo, *, remote="origin", env_overlay=None):  # type: ignore[no-untyped-def]
        return True

    async def fake_exists(main_repo, branch, *, remote="origin", env_overlay=None):  # type: ignore[no-untyped-def]
        captured["overlay"] = env_overlay
        return True

    async def stop_after_check(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("stop-after-branch-check")

    monkeypatch.setattr(alloc, "_fetch", fake_fetch)
    monkeypatch.setattr(alloc, "_remote_branch_exists", fake_exists)
    monkeypatch.setattr(alloc, "_existing_worktree_for_path", stop_after_check)

    overlay = {"GIT_CONFIG_COUNT": "3"}
    with pytest.raises(RuntimeError, match="stop-after-branch-check"):
        await alloc.ensure_worktree(
            main_repo=tmp_path,
            worktree_path=tmp_path / "wt",
            branch_name="feature",
            base_ref="origin/feature",
            fetch=True,
            fetch_env_overlay=overlay,
        )
    assert captured["overlay"] == overlay
