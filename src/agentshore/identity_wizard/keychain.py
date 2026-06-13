"""OS keychain / keyring helpers for the identity wizard.

All keyring operations route through :mod:`agentshore.keyring_child` — the
single killable child-process implementation — so the "keyring can wedge" fix
applies uniformly.  Functions that only need to introspect the backend *type*
(for UI labels) still do a direct non-blocking import; they never call
``get_password`` or ``set_password`` directly.
"""

from __future__ import annotations

import sys

from agentshore import keyring_child
from agentshore.identity_names import (
    canonical_keychain_service,
    keychain_service_for_login,
    keychain_service_for_repo_login,
)


def _keychain_backend_label() -> str | None:
    """Return a human-readable keyring backend label, or ``None`` if unavailable."""
    backend_name = keyring_child.keyring_backend_name()
    if not backend_name or backend_name.startswith("unavailable"):
        return None
    # "FailKeyring" is the keyring sentinel that means "no working backend".
    if "fail" in backend_name.casefold():
        return None
    if sys.platform == "darwin":
        return f"macOS Keychain ({backend_name})"
    if sys.platform.startswith("win"):
        return f"Windows Credential Manager ({backend_name})"
    return f"OS credential store ({backend_name})"


def _store_in_keychain(service: str, token: str) -> tuple[bool, str]:
    try:
        keyring_child.keyring_set(service, token)
    except keyring_child.KeyringTimeoutError as exc:
        return False, f"keyring write timed out: {exc}"
    except RuntimeError as exc:
        return False, f"keyring write failed: {exc}"
    return True, f"Stored under service {service!r} ({_keychain_backend_label()})"


def _migrate_keychain_token(from_service: str, to_service: str) -> bool:
    """Copy a token from one keychain service to another. Returns True on success."""
    token = keyring_child.keyring_get(from_service)
    if not token or not token.strip():
        return False
    try:
        keyring_child.keyring_set(to_service, token)
    except Exception:
        return False
    return True


def _keychain_has_token(service: str) -> bool:
    return keyring_child.keychain_has_token(service)


def _managed_keychain_service(login: str, repo_name_with_owner: str | None) -> str:
    if repo_name_with_owner:
        return keychain_service_for_repo_login(repo_name_with_owner, login)
    return keychain_service_for_login(login)


def _agentshore_managed_service(service: str) -> bool:
    return canonical_keychain_service(service).startswith("agentshore/")
