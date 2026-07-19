"""Tests for the ``beads.designate_migrator`` RPC handler (gh-356)."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agentshore.sidecar.rpc.protocol import ERR_NO_ACTIVE_PROJECT, INTERNAL_ERROR
from agentshore.sidecar.server import ServerState, handle_request


def _resolve(response: object) -> object:
    """Resolve a possibly-awaitable handle_request result for sync RPC tests."""
    if inspect.isawaitable(response):
        return asyncio.run(response)
    return response


def test_designate_migrator_runs_consented_reconcile(tmp_path: Path) -> None:
    """A successful reconcile with assume_yes=True returns {designated: True}."""
    state = ServerState(active_project_path=str(tmp_path))
    fake = AsyncMock(return_value=None)

    with patch("agentshore.beads.setup.reconcile_beads_schema", fake):
        response = _resolve(
            handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "beads.designate_migrator",
                    "params": {},
                },
                state=state,
            )
        )

    assert response is not None
    assert response["result"] == {"designated": True}
    fake.assert_awaited_once()
    # The consent flag is the whole point — it must be assume_yes=True.
    assert fake.await_args.args == (tmp_path,)
    assert fake.await_args.kwargs == {"assume_yes": True}


def test_designate_migrator_requires_active_project() -> None:
    """With no active project selected, the handler refuses rather than acting
    on the sidecar cwd — this mutates a shared store."""
    state = ServerState(active_project_path=None)

    response = _resolve(
        handle_request(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "beads.designate_migrator",
                "params": {},
            },
            state=state,
        )
    )

    assert response is not None
    assert response["error"]["code"] == ERR_NO_ACTIVE_PROJECT


def test_designate_migrator_surfaces_migration_failure(tmp_path: Path) -> None:
    """A BeadsSchemaDriftError from the consented migrate+push becomes a
    JSON-RPC error, not a silent success."""
    from agentshore.beads import BeadsSchemaDriftError

    state = ServerState(active_project_path=str(tmp_path))
    fake = AsyncMock(side_effect=BeadsSchemaDriftError("migrate+push failed"))

    with patch("agentshore.beads.setup.reconcile_beads_schema", fake):
        response = _resolve(
            handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "beads.designate_migrator",
                    "params": {},
                },
                state=state,
            )
        )

    assert response is not None
    assert response["error"]["code"] == INTERNAL_ERROR
    assert "migrate+push failed" in response["error"]["message"]
