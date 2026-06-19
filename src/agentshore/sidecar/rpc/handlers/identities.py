"""Handler for the ``identities.*`` method family."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from agentshore.sidecar.identities import (
    add_identity,
    add_trusted_source,
    check_identity_access,
    keychain_status,
    list_identities,
    list_trusted_sources,
    remove_identity,
    remove_trusted_source,
    update_identity,
)
from agentshore.sidecar.rpc.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    DispatchResult,
    JsonRpcNotification,
    JsonRpcResponse,
    ServerState,
    _error,
    _result,
)


def _dispatch_identities_rpc(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
    active_project_path: Path,
) -> DispatchResult:
    if method == "identities.list":
        try:
            return _result(req_id, list_identities(active_project_path))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"identities.list: {exc}")

    if method == "identities.check_keychain":
        if not isinstance(raw_params, dict):
            return _error(
                req_id, INVALID_PARAMS, "identities.check_keychain requires object params"
            )
        login = raw_params.get("login")
        if not isinstance(login, str):
            return _error(req_id, INVALID_PARAMS, "identities.check_keychain requires login")
        keychain_login: str = login

        # Run off the serve loop: keychain_status spawns a killable child
        # subprocess that can take seconds (Windows Credential Manager under
        # antivirus). Returning a coroutine lets the dispatcher schedule it as a
        # task so concurrent setup-screen RPCs don't serialize behind it — the
        # Windows "nearly every screen times out" cascade.
        async def _run_check_keychain() -> JsonRpcResponse:
            try:
                return _result(req_id, await asyncio.to_thread(keychain_status, keychain_login))
            except ValueError as exc:
                return _error(req_id, INVALID_PARAMS, str(exc))

        return _run_check_keychain()

    if method == "identities.check_access":
        if not isinstance(raw_params, dict):
            return _error(req_id, INVALID_PARAMS, "identities.check_access requires object params")
        login = raw_params.get("login")
        if not isinstance(login, str):
            return _error(req_id, INVALID_PARAMS, "identities.check_access requires login")
        access_login: str = login
        _project_path = active_project_path

        # Off the serve loop (see check_keychain): check_identity_access is async
        # and runs its blocking gh/keyring/repo-access calls inside threads
        # internally.  The Identities screen fires one per configured identity
        # concurrently; they complete in parallel because each awaits its own
        # to_thread call instead of serialising through a single thread pool entry.
        async def _run_check_access() -> JsonRpcResponse:
            try:
                return _result(
                    req_id,
                    await check_identity_access(_project_path, access_login),
                )
            except ValueError as exc:
                return _error(req_id, INVALID_PARAMS, str(exc))
            except OSError as exc:
                return _error(req_id, INTERNAL_ERROR, f"identities.check_access: {exc}")

        return _run_check_access()

    if method == "identities.add":
        if not isinstance(raw_params, dict):
            return _error(req_id, INVALID_PARAMS, "identities.add requires object params")
        login = raw_params.get("login")
        token_source = raw_params.get("token_source")
        if not isinstance(login, str) or not isinstance(token_source, str):
            return _error(req_id, INVALID_PARAMS, "identities.add requires login and token_source")
        pat = raw_params.get("pat")
        if pat is not None and not isinstance(pat, str):
            return _error(req_id, INVALID_PARAMS, "identities.add: 'pat' must be a string")
        try:
            add_identity(active_project_path, login, token_source, pat=pat or None)
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"identities.add: {exc}")
        return _result(req_id, {})

    if method == "identities.update":
        if not isinstance(raw_params, dict):
            return _error(req_id, INVALID_PARAMS, "identities.update requires object params")
        login = raw_params.get("login")
        patch = raw_params.get("patch")
        if not isinstance(login, str) or not isinstance(patch, dict):
            return _error(req_id, INVALID_PARAMS, "identities.update requires login and patch")
        try:
            update_identity(active_project_path, login, patch)
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"identities.update: {exc}")
        return _result(req_id, {})

    if method == "identities.remove":
        if not isinstance(raw_params, dict):
            return _error(req_id, INVALID_PARAMS, "identities.remove requires object params")
        login = raw_params.get("login")
        if not isinstance(login, str):
            return _error(req_id, INVALID_PARAMS, "identities.remove requires login")
        try:
            remove_identity(active_project_path, login)
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"identities.remove: {exc}")
        return _result(req_id, {})

    if method == "identities.list_trusted":
        try:
            return _result(req_id, list_trusted_sources(active_project_path))
        except (OSError, ValueError) as exc:
            return _error(req_id, INTERNAL_ERROR, f"identities.list_trusted: {exc}")

    if method in ("identities.add_trusted", "identities.remove_trusted"):
        if not isinstance(raw_params, dict):
            return _error(req_id, INVALID_PARAMS, f"{method} requires object params")
        login = raw_params.get("login")
        if not isinstance(login, str):
            return _error(req_id, INVALID_PARAMS, f"{method} requires login")
        op = add_trusted_source if method == "identities.add_trusted" else remove_trusted_source
        try:
            op(active_project_path, login)
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"{method}: {exc}")
        return _result(req_id, {})

    return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")
