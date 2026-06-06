"""Regression: the sidecar must boot the orchestrator with a config_path.

Without it, ``orch._config_path`` is None and ``Orchestrator.set_budget(
persist=True)`` silently no-ops — a desktop "Adjust Budget…" change would not
survive a restart (it only lived as an in-memory override). See #41/#43.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agentshore.core as core
from agentshore.config.models import RuntimeConfig
from agentshore.sidecar.server import ServerState


class _StopBootstrapError(Exception):
    pass


@pytest.mark.asyncio
async def test_start_orchestrator_passes_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentshore.sidecar import session_lifecycle

    captured: dict[str, object] = {}

    async def _fake_bootstrap(**kwargs: object) -> object:
        captured.update(kwargs)
        raise _StopBootstrapError  # short-circuit before the heavy run-loop wiring

    monkeypatch.setattr(core.Orchestrator, "bootstrap", _fake_bootstrap)

    with pytest.raises(_StopBootstrapError):
        await session_lifecycle._start_orchestrator(
            state=ServerState(),
            project_path=tmp_path,
            cfg=RuntimeConfig(),
            session_id="sess-1",
            notify=None,
            first_snapshot_timeout_seconds=0.1,
        )

    assert captured["config_path"] == tmp_path / "agentshore.yaml"
    assert captured["embedded_mode"] is True
