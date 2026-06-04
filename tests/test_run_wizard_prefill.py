"""Tests for the identity wizard's `defaults` prefill path.

Regression for the 2026-05-07 init-wizard bug: re-running the identity
wizard ignored existing `identities:` bindings, forcing the user to re-pick
their account every time. The `defaults` kwarg now drives default_idx +
adds a `(current)` annotation, with `(was X — re-pick)` for stale logins.
"""

from __future__ import annotations

from unittest.mock import patch

from agentshore.identity_wizard.gh_accounts import GhAccount
from agentshore.identity_wizard.wizard import run_wizard


def _accounts() -> list[GhAccount]:
    return [
        GhAccount(login="alice", active=True),
        GhAccount(login="bob", active=False),
    ]


def test_picker_label_unchanged_without_defaults() -> None:
    """No defaults → no `(current)` annotation in the picker label."""
    with patch(
        "agentshore.identity_wizard.wizard._prompt_choice", return_value=None
    ) as mock_prompt:
        run_wizard(["claude_code"], accounts=_accounts())

    label = mock_prompt.call_args.args[0]
    assert "current" not in label
    assert "was " not in label


def test_picker_shows_current_marker_when_default_matches_login() -> None:
    """defaults[agent] = login that exists in accounts → `(current: X)` annotation."""
    with patch(
        "agentshore.identity_wizard.wizard._prompt_choice", return_value=None
    ) as mock_prompt:
        run_wizard(
            ["claude_code"],
            accounts=_accounts(),
            defaults={"claude_code": "bob"},
        )

    label, logins, default_idx, *_ = mock_prompt.call_args.args
    assert "current: bob" in label
    # Default index points to bob (index 1)
    assert logins[default_idx] == "bob"


def test_picker_shows_was_marker_when_default_login_no_longer_authenticated() -> None:
    """A previously-bound login that's no longer in gh auth and not in
    existing_identities → `(was X — re-pick)`."""
    with patch(
        "agentshore.identity_wizard.wizard._prompt_choice", return_value=None
    ) as mock_prompt:
        run_wizard(
            ["claude_code"],
            accounts=_accounts(),
            defaults={"claude_code": "stale_login"},
        )

    label, logins, default_idx, *_ = mock_prompt.call_args.args
    assert "was stale_login" in label
    # Falls back to active account
    assert logins[default_idx] == "alice"


def test_picker_surfaces_keychain_only_login_from_existing_identities() -> None:
    """A login present in existing_identities but not in gh auth (keychain-only)
    must appear as a picker candidate, not be flagged as stale.

    Regression for the 2026-05-07 wizard bug: keychain-stored identities like
    `unseriousAI` (PAT in macOS Keychain) disappeared from the picker because
    `detect_gh_accounts()` only sees logins from `gh auth status`.
    """
    from agentshore.identity_wizard import IdentityBinding

    existing = {
        "unseriousAI": IdentityBinding(
            name="unseriousAI",
            git_user_name="UnseriousAI",
            git_user_email="bot@example.com",
            gh_token_keychain="agentshore/unseriousAI",
        )
    }

    with patch(
        "agentshore.identity_wizard.wizard._prompt_choice", return_value=None
    ) as mock_prompt:
        run_wizard(
            ["claude_code"],
            accounts=_accounts(),
            defaults={"claude_code": "unseriousAI"},
            existing_identities=existing,
        )

    label, logins, default_idx, *_ = mock_prompt.call_args.args
    # The keychain-only login appears under AgentShore's canonical machine key.
    assert "unseriousai" in logins
    # And it must be the pre-selected default (not "stale").
    assert logins[default_idx] == "unseriousai"
    assert "current: unseriousAI" in label
    assert "was " not in label


def test_keep_existing_skips_step2_prompts_for_keychain_identity(tmp_path) -> None:
    """When an identity is already configured (keychain), Step 2 offers a
    `Keep existing settings?` shortcut so the user doesn't have to re-paste
    the PAT or pick a strategy again.
    """
    from agentshore.identity_wizard import IdentityBinding

    existing = {
        "unseriousAI": IdentityBinding(
            name="unseriousAI",
            git_user_name="UnseriousAI",
            git_user_email="bot@example.com",
            gh_token_keychain="agentshore/unseriousAI",
        )
    }

    # Step 1 picker → choose unseriousAI (the second-and-only configured-only entry).
    # Step 2 → confirm the "keep existing" prompt (default True).
    with (
        patch("agentshore.identity_wizard.wizard._prompt_choice", return_value="unseriousAI"),
        patch("agentshore.identity_wizard.wizard.click.confirm", return_value=True) as mock_confirm,
    ):
        result = run_wizard(
            ["claude_code"],
            accounts=_accounts(),
            defaults={"claude_code": "unseriousAI"},
            existing_identities=existing,
        )

    # Step 2 confirm was invoked at least once (the "Keep existing settings?" prompt).
    assert mock_confirm.called
    # Result preserved the existing keychain binding verbatim.
    assert result.agent_to_identity == {"claude_code": "unseriousai"}
    binding = result.identities["unseriousai"]
    assert binding.gh_token_keychain == "agentshore/unseriousai"
    assert binding.gh_token_login is None
    assert binding.gh_token_env is None
    assert binding.git_user_email == "bot@example.com"


def test_per_agent_defaults_are_independent() -> None:
    """Each agent's prefill is computed against its own default."""
    calls = []

    def fake_prompt(label, logins, default_idx, **kwargs):
        calls.append((label, logins[default_idx]))
        return None

    with patch("agentshore.identity_wizard.wizard._prompt_choice", side_effect=fake_prompt):
        run_wizard(
            ["claude_code", "codex"],
            accounts=_accounts(),
            defaults={"claude_code": "bob", "codex": "alice"},
        )

    assert calls[0][1] == "bob"
    assert calls[1][1] == "alice"
    assert "current: bob" in calls[0][0]
    assert "current: alice" in calls[1][0]
