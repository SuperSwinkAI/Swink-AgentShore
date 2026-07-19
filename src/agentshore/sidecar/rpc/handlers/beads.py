"""Handler for the ``beads.*`` method family.

Currently a single method, ``beads.designate_migrator`` — the desktop's
response to the ``$/beads_schema_drift`` notification (issue #356). When a
remote-backed beads store is behind its schema and ``bd bootstrap`` can't
catch it up, ``reconcile_beads_schema`` (``agentshore.beads.setup``) declines
to migrate headlessly because that migrate+push is the one action that can
unrecoverably fork a shared schema. This RPC is the user answering "yes, this
machine is the designated migrator": it re-runs the reconcile with explicit
consent (``assume_yes=True``), which performs the migrate+push and durably
marks this clone so every future session auto-migrates silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from agentshore.sidecar.rpc.protocol import (
    ERR_NO_ACTIVE_PROJECT,
    INTERNAL_ERROR,
    DispatchResult,
    JsonRpcNotification,
    JsonRpcResponse,
    ServerState,
    _error,
    _result,
)


def _dispatch_beads_rpc(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    if method == "beads.designate_migrator":
        return _dispatch_designate_migrator(req_id, state)

    from agentshore.sidecar.rpc.protocol import METHOD_NOT_FOUND

    return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")


def _dispatch_designate_migrator(
    req_id: int | str | None,
    state: ServerState,
) -> DispatchResult:
    """``beads.designate_migrator`` — consent to the remote schema migrate+push.

    Resolves the active project explicitly (no cwd fallback — this mutates a
    shared store, so acting on an unrelated cwd would be dangerous): returns
    ``ERR_NO_ACTIVE_PROJECT`` when no project is selected. Otherwise runs
    ``reconcile_beads_schema(project_path, assume_yes=True)`` off the serve
    loop (it shells out to ``bd migrate``/``bd dolt push``, which can take a
    while over a network remote), translating any failure to a JSON-RPC error.
    """
    active = state.active_project_path
    if active is None:
        return _error(
            req_id,
            ERR_NO_ACTIVE_PROJECT,
            "beads.designate_migrator requires an active project",
        )

    from pathlib import Path

    project_path: Path = Path(active)

    async def _run() -> JsonRpcResponse:
        from agentshore.beads import BeadsSchemaDriftError
        from agentshore.beads.setup import reconcile_beads_schema

        try:
            await reconcile_beads_schema(project_path, assume_yes=True)
        except BeadsSchemaDriftError as exc:
            return _error(req_id, INTERNAL_ERROR, f"schema migration failed: {exc}")
        except Exception as exc:  # pragma: no cover — defensive guard
            return _error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return _result(req_id, {"designated": True})

    return _run()
