"""Session-boundary durability push (``bd dolt push``)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentshore.beads import BdError
from agentshore.beads.durability import push_beads_remote


@pytest.mark.asyncio
async def test_push_beads_remote_logs_ok_and_returns_true(tmp_path: Path) -> None:
    with patch(
        "agentshore.beads.durability.bd",
        new=AsyncMock(return_value="Everything up-to-date\n"),
    ) as mock_bd:
        result = await push_beads_remote(tmp_path)

    assert result is True
    mock_bd.assert_awaited_once()
    args, kwargs = mock_bd.await_args
    assert args == ("dolt", "push")
    assert kwargs["cwd"] == tmp_path


@pytest.mark.asyncio
async def test_push_beads_remote_treats_no_remote_as_success(tmp_path: Path) -> None:
    with patch(
        "agentshore.beads.durability.bd",
        new=AsyncMock(return_value="No remote is configured — skipping.\n"),
    ):
        result = await push_beads_remote(tmp_path)

    assert result is True


@pytest.mark.asyncio
async def test_push_beads_remote_never_raises_on_bd_error(tmp_path: Path) -> None:
    with patch(
        "agentshore.beads.durability.bd",
        new=AsyncMock(side_effect=BdError("bd dolt push failed (rc=1): network unreachable")),
    ):
        result = await push_beads_remote(tmp_path)

    assert result is False
