"""Contract tests for the injected CLI dispatch collaborators."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentshore.agents._jsonl import _UsageTotals
from agentshore.agents.cli.drivers import (
    DEFAULT_CLI_DRIVERS,
    AntigravityCliDriver,
    GrokCliDriver,
    SwinkCodingCliDriver,
)
from agentshore.agents.cli.parsing import _ReadOutput
from agentshore.agents.cli.supervisor import (
    ProcessRunRequest,
    SupervisedProcessResult,
)
from agentshore.agents.cli.watchdogs import _StderrSniffer
from agentshore.agents.cli_agent import dispatch_cli
from agentshore.agents.handle import AgentHandle
from agentshore.config import AgentConfig
from agentshore.state import AgentStatus, AgentType


class _FakeSupervisor:
    def __init__(self) -> None:
        self.request: ProcessRunRequest | None = None

    async def run(
        self,
        request: ProcessRunRequest,
        *,
        handle: AgentHandle,
        cfg: AgentConfig,
        on_subprocess_spawned: object = None,
        on_subprocess_exited: object = None,
    ) -> SupervisedProcessResult:
        self.request = request
        process = SimpleNamespace(returncode=0, pid=8181, _transport=None)
        return SupervisedProcessResult(
            process=process,  # type: ignore[arg-type]
            output=_ReadOutput(
                raw="completed",
                usage=_UsageTotals(tokens_in=12, tokens_out=4),
                session_id="session-from-output",
            ),
            post_response_killed=False,
            stderr_sniffer=_StderrSniffer(),
        )


@pytest.mark.asyncio
async def test_dispatch_cli_delegates_process_lifecycle_to_injected_supervisor(
    tmp_path: Path,
) -> None:
    supervisor = _FakeSupervisor()
    handle = AgentHandle(
        agent_id="codex-1",
        agent_type=AgentType.CODEX,
        status=AgentStatus.IDLE,
        working_dir=tmp_path,
    )

    result = await dispatch_cli(
        handle,
        "do the work",
        cfg=AgentConfig(enabled=True, binary="codex", timeout=10),
        supervisor=supervisor,
    )

    assert supervisor.request is not None
    assert supervisor.request.cwd == tmp_path
    assert supervisor.request.argv[:3] == ("codex", "exec", "--json")
    assert result.raw_output == "completed"
    assert result.tokens_in == 12
    assert result.session_id == "session-from-output"


def test_driver_registry_selects_provider_specific_drivers() -> None:
    assert isinstance(DEFAULT_CLI_DRIVERS.driver_for(AgentType.GROK), GrokCliDriver)
    assert isinstance(
        DEFAULT_CLI_DRIVERS.driver_for(AgentType.ANTIGRAVITY),
        AntigravityCliDriver,
    )
    assert isinstance(
        DEFAULT_CLI_DRIVERS.driver_for(AgentType.SWINK_CODING),
        SwinkCodingCliDriver,
    )
    assert type(DEFAULT_CLI_DRIVERS.driver_for(AgentType.CODEX)) is not GrokCliDriver
