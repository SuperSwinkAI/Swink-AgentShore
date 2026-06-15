"""Tests for canonical identity and keychain naming helpers."""

from __future__ import annotations

import pytest

from agentshore.identity_names import (
    canonical_keychain_service,
    canonical_repo_name_with_owner,
    keychain_service_for_login,
    keychain_service_for_repo_login,
    login_from_agentshore_keychain_service,
)


def test_keychain_service_for_login_is_legacy_global_service() -> None:
    assert keychain_service_for_login("bot-user") == "agentshore/bot-user"


def test_keychain_service_for_repo_login_scopes_by_repo() -> None:
    assert (
        keychain_service_for_repo_login("example-user/example-repo", "bot-user")
        == "agentshore/example-user/example-repo/bot-user"
    )


def test_canonical_keychain_service_lowercases_repo_scoped_service() -> None:
    assert (
        canonical_keychain_service("agentshore/EXAMPLE-USER/Example-Repo/bot-user")
        == "agentshore/example-user/example-repo/bot-user"
    )


def test_login_from_agentshore_keychain_service_handles_legacy_and_repo_scoped() -> None:
    assert login_from_agentshore_keychain_service("agentshore/bot-user") == "bot-user"
    assert (
        login_from_agentshore_keychain_service("agentshore/example-user/example-repo/bot-user")
        == "bot-user"
    )
    assert login_from_agentshore_keychain_service("custom/bot-user") is None


def test_canonical_repo_name_with_owner_rejects_non_repo_shape() -> None:
    with pytest.raises(ValueError, match="owner/repo"):
        canonical_repo_name_with_owner("example-user")
