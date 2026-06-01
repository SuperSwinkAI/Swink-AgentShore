"""OS keychain / keyring helpers for the identity wizard.

All keyring imports are lazy so the wizard degrades gracefully when the
``keyring`` library or an OS credential backend is unavailable.
"""

from __future__ import annotations

import sys

from agentshore.identity_names import (
    canonical_keychain_service,
    keychain_service_for_login,
    keychain_service_for_repo_login,
)


def _keychain_backend_label() -> str | None:
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except ImportError:
        return None
    backend = keyring.get_keyring()
    if isinstance(backend, FailKeyring):
        return None
    cls = type(backend).__name__
    if sys.platform == "darwin":
        return f"macOS Keychain ({cls})"
    if sys.platform.startswith("win"):
        return f"Windows Credential Manager ({cls})"
    return f"OS credential store ({cls})"


def _store_in_keychain(service: str, token: str) -> tuple[bool, str]:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return False, "keyring library not installed"
    try:
        keyring.set_password(service, service, token)
    except KeyringError as exc:
        return False, f"keyring write failed: {exc}"
    return True, f"Stored under service {service!r} ({_keychain_backend_label()})"


def _migrate_keychain_token(from_service: str, to_service: str) -> bool:
    """Copy a token from one keychain service to another. Returns True on success."""
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return False
    try:
        token = keyring.get_password(from_service, from_service)
    except KeyringError:
        return False
    if not token or not token.strip():
        return False
    try:
        keyring.set_password(to_service, to_service, token)
    except KeyringError:
        return False
    return True


def _keychain_has_token(service: str) -> bool:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return False
    try:
        token = keyring.get_password(service, service)
    except KeyringError:
        return False
    return bool(token and token.strip())


def _managed_keychain_service(login: str, repo_name_with_owner: str | None) -> str:
    if repo_name_with_owner:
        return keychain_service_for_repo_login(repo_name_with_owner, login)
    return keychain_service_for_login(login)


def _agentshore_managed_service(service: str) -> bool:
    return canonical_keychain_service(service).startswith("agentshore/")
