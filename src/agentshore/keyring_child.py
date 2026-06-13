"""Killable keyring child-process accessor.

Keyring backends (Windows Credential Manager, macOS Keychain) can wedge inside
OS credential discovery — for example when discovering the active desktop
session on a freshly-unlocked Windows box.  A daemon thread can return a
timeout to the UI but the backend call keeps running inside the sidecar forever.
A subprocess gives the timeout real teeth: Python kills and reaps the child when
``subprocess.run(..., timeout=...)`` expires.

This module owns the one canonical killable keyring implementation.  Call sites
in ``sidecar/identities.py``, ``agents/identity.py``, and
``identity_wizard/keychain.py`` all route through here so the "keyring can
wedge" fix is applied exactly once.
"""

from __future__ import annotations

import json
import subprocess
import sys

from agentshore import subprocess_env

_KEYRING_CHILD_CODE = r"""
import json
import sys

request = json.loads(sys.stdin.read() or "{}")
try:
    import keyring

    op = request.get("op")
    service = str(request.get("service") or "")
    if op == "get":
        token = keyring.get_password(service, service)
        print(json.dumps({"ok": True, "token": token}))
    elif op == "set":
        keyring.set_password(service, service, str(request.get("token") or ""))
        print(json.dumps({"ok": True}))
    elif op == "backend":
        print(json.dumps({"ok": True, "backend": type(keyring.get_keyring()).__name__}))
    else:
        print(json.dumps({"ok": False, "error": f"unsupported op: {op}"}))
except Exception as exc:
    print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
"""


class KeyringTimeoutError(RuntimeError):
    """Raised when the local OS credential backend does not answer promptly."""


def run_keyring_child(request: dict[str, object]) -> dict[str, object]:
    """Run one OS credential operation in a killable child process.

    Supported *request* ops:
    - ``{"op": "get", "service": "<name>"}`` — returns ``{"ok": True, "token": str | None}``
    - ``{"op": "set", "service": "<name>", "token": "<value>"}`` — returns ``{"ok": True}``
    - ``{"op": "backend"}`` — returns ``{"ok": True, "backend": "<class name>"}``

    Raises :exc:`KeyringTimeoutError` when the child does not respond within
    the AV-scaled ``timeout_for("keyring")`` window.  Raises :exc:`RuntimeError`
    for any other failure (spawn failure, JSON decode error, non-ok response).
    """
    try:
        # input= provides the JSON payload via stdin pipe; subprocess.run handles
        # the pipe internally so we do not pass a separate stdin= argument.
        proc = subprocess.run(  # noqa: S603 — same Python executable, fixed -c payload
            [sys.executable, "-c", _KEYRING_CHILD_CODE],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            check=False,
            timeout=subprocess_env.timeout_for("keyring"),
            creationflags=subprocess_env.no_window_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise KeyringTimeoutError("keyring operation timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"keyring child failed to start: {exc}") from exc

    try:
        payload = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("keyring child returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("keyring child returned non-object JSON")
    if payload.get("ok") is True:
        return payload
    error = payload.get("error")
    raise RuntimeError(str(error or "keyring operation failed"))


def keyring_get(service: str) -> str | None:
    """Read a stored OS credential for *service*, returning ``None`` on any failure."""
    try:
        result = run_keyring_child({"op": "get", "service": service})
        token = result.get("token")
        return token if isinstance(token, str) else None
    except Exception:
        return None


def keyring_set(service: str, token: str) -> None:
    """Store *token* in the OS credential store.

    Raises :exc:`KeyringTimeoutError` when the backend wedges, or
    :exc:`RuntimeError` / :exc:`ValueError` for other failures.
    """
    try:
        run_keyring_child({"op": "set", "service": service, "token": token})
    except KeyringTimeoutError:
        raise
    except Exception as exc:
        raise RuntimeError(f"failed to store token in credential store: {exc}") from exc


def keyring_backend_name() -> str:
    """Return the keyring backend class name, or ``'unknown'`` / ``'unavailable (...)'``."""
    try:
        result = run_keyring_child({"op": "backend"})
        backend = result.get("backend")
        return backend if isinstance(backend, str) and backend else "unknown"
    except Exception as exc:  # pragma: no cover — backend discovery varies by OS
        return f"unavailable ({type(exc).__name__})"


def keychain_has_token(service: str) -> bool:
    """Return ``True`` when the OS credential store has a non-empty token for *service*."""
    token = keyring_get(service)
    return bool(token and token.strip())
