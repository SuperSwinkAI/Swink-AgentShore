"""Live stdout reading and parser handoff for CLI subprocesses."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from agentshore.agents._jsonl import _UsageTotals
from agentshore.agents.cli.parsing import _PARSERS, _is_terminal_event, _ReadOutput
from agentshore.agents.cli.watchdogs import _ReadOutputFailed, _StdoutActivity
from agentshore.errors import AgentOutputInvalid
from agentshore.logging import get_logger

if TYPE_CHECKING:
    from agentshore.state import AgentType

_logger = get_logger(__name__)
_LINE_DRIFT_WARN_BYTES = 1_048_576


async def read_output(
    proc: asyncio.subprocess.Process,
    agent_type: AgentType,
    max_bytes: int,
    *,
    line_limit: int,
    agent_id: str,
    stdout_activity: _StdoutActivity | None = None,
) -> _ReadOutput:
    """Stream stdout, enforce size limits, and parse provider output."""
    if proc.stdout is None:
        msg = "Subprocess stdout is None after create_subprocess_exec"
        raise RuntimeError(msg)
    chunks: list[bytes] = []
    total_bytes = 0
    drift_warned = False

    try:
        async for line in proc.stdout:
            if (
                stdout_activity is not None
                and stdout_activity.mark()
                and stdout_activity.dispatch_start
                and stdout_activity.first_byte_at
            ):
                _logger.info(
                    "cli_first_byte",
                    agent_id=agent_id,
                    agent_type=str(agent_type),
                    elapsed_ms=int(
                        (stdout_activity.first_byte_at - stdout_activity.dispatch_start) * 1000
                    ),
                )
            total_bytes += len(line)
            if total_bytes > max_bytes:
                raise AgentOutputInvalid(
                    f"agent output exceeded {max_bytes} bytes (max_output_size)"
                )
            if not drift_warned and len(line) >= _LINE_DRIFT_WARN_BYTES:
                drift_warned = True
                _logger.warning(
                    "cli_agent_large_line",
                    agent_id=agent_id,
                    agent_type=str(agent_type),
                    line_bytes=len(line),
                    line_limit=line_limit,
                )
            chunks.append(line)

            if (
                stdout_activity is not None
                and not stdout_activity.response_complete
                and _is_terminal_event(line, agent_type)
            ):
                stdout_activity.mark_response_complete()
    except asyncio.LimitOverrunError as exc:
        raise AgentOutputInvalid(
            f"agent {agent_id!r} stream-json line exceeded {line_limit} bytes "
            f"(consumed={exc.consumed}); raise agents.<name>.line_limit_bytes "
            f"in agentshore.yaml"
        ) from exc
    except ValueError as exc:
        msg = str(exc)
        if "chunk" in msg and "limit" in msg:
            raise AgentOutputInvalid(
                f"agent {agent_id!r} stream-json line exceeded {line_limit} bytes; "
                f"raise agents.<name>.line_limit_bytes in agentshore.yaml"
            ) from exc
        raise

    raw = b"".join(chunks).decode("utf-8", errors="replace")
    parser = _PARSERS.get(agent_type)
    if parser is not None:
        raw, usage, session_id = parser.parse(raw)
    else:
        usage = _UsageTotals()
        session_id = None

    await proc.wait()
    return _ReadOutput(raw=raw, usage=usage, session_id=session_id)


async def read_output_guarded(
    proc: asyncio.subprocess.Process,
    agent_type: AgentType,
    max_bytes: int,
    *,
    line_limit: int,
    agent_id: str,
    stdout_activity: _StdoutActivity,
) -> _ReadOutput | _ReadOutputFailed:
    """Return read failures as values so watchdog races can clean up uniformly."""
    try:
        return await read_output(
            proc,
            agent_type,
            max_bytes,
            line_limit=line_limit,
            agent_id=agent_id,
            stdout_activity=stdout_activity,
        )
    except BaseException as exc:
        return _ReadOutputFailed(exc)


__all__ = ["read_output", "read_output_guarded"]
