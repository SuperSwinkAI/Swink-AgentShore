"""Provider-specific preparation and finalization for CLI dispatches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agentshore.agents import cli_antigravity, cli_swink_coding
from agentshore.agents.cli.argv import _prompt_on_stdin, _write_grok_prompt_file
from agentshore.state import AgentType

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class CliRunPreparation:
    """Provider-owned resources and identifiers prepared before argv construction."""

    prompt_file: Path | None = None
    pinned_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CliProviderOutput:
    """Provider-normalized output returned to the generic dispatcher."""

    raw_output: str
    session_id: str | None


class CliDriver(Protocol):
    """Provider hooks surrounding the shared subprocess lifecycle."""

    def prepare(
        self,
        prompt: str,
        *,
        python_executable: str | None,
        resume_session_id: str | None,
    ) -> CliRunPreparation: ...

    def finalize(
        self,
        raw_output: str,
        observed_session_id: str | None,
        *,
        preparation: CliRunPreparation,
        effective_cwd: Path,
        env: dict[str, str],
    ) -> CliProviderOutput: ...

    def cleanup(self, preparation: CliRunPreparation) -> None: ...


class DefaultCliDriver:
    """No-op hooks for providers that use the common CLI contract directly."""

    def prepare(
        self,
        prompt: str,
        *,
        python_executable: str | None,
        resume_session_id: str | None,
    ) -> CliRunPreparation:
        del prompt, python_executable, resume_session_id
        return CliRunPreparation()

    def finalize(
        self,
        raw_output: str,
        observed_session_id: str | None,
        *,
        preparation: CliRunPreparation,
        effective_cwd: Path,
        env: dict[str, str],
    ) -> CliProviderOutput:
        del effective_cwd, env
        return CliProviderOutput(
            raw_output=raw_output,
            session_id=observed_session_id or preparation.pinned_session_id,
        )

    def cleanup(self, preparation: CliRunPreparation) -> None:
        if preparation.prompt_file is not None:
            preparation.prompt_file.unlink(missing_ok=True)


class GrokCliDriver(DefaultCliDriver):
    """Route oversized Windows prompts through Grok's prompt-file option."""

    def prepare(
        self,
        prompt: str,
        *,
        python_executable: str | None,
        resume_session_id: str | None,
    ) -> CliRunPreparation:
        del resume_session_id
        prompt_file = (
            _write_grok_prompt_file(prompt) if _prompt_on_stdin(python_executable) else None
        )
        return CliRunPreparation(prompt_file=prompt_file)


class SwinkCodingCliDriver(DefaultCliDriver):
    """Pin new swink-coding sessions so malformed output remains resumable."""

    def prepare(
        self,
        prompt: str,
        *,
        python_executable: str | None,
        resume_session_id: str | None,
    ) -> CliRunPreparation:
        del prompt
        pinned_session_id = (
            cli_swink_coding.new_pinned_session_id()
            if resume_session_id is None and python_executable is None
            else None
        )
        return CliRunPreparation(pinned_session_id=pinned_session_id)


class AntigravityCliDriver(DefaultCliDriver):
    """Normalize Antigravity's task envelope and recover its cached conversation id."""

    def finalize(
        self,
        raw_output: str,
        observed_session_id: str | None,
        *,
        preparation: CliRunPreparation,
        effective_cwd: Path,
        env: dict[str, str],
    ) -> CliProviderOutput:
        raw_output = cli_antigravity.extract_output(raw_output)
        if observed_session_id is None:
            observed_session_id = cli_antigravity.resolve_conversation_id(
                effective_cwd,
                home=env.get("HOME"),
            )
        return super().finalize(
            raw_output,
            observed_session_id,
            preparation=preparation,
            effective_cwd=effective_cwd,
            env=env,
        )


class CliDriverRegistry:
    """Resolve one immutable driver object for each supported agent type."""

    def __init__(self) -> None:
        default = DefaultCliDriver()
        self._drivers: dict[AgentType, CliDriver] = {
            AgentType.CLAUDE_CODE: default,
            AgentType.CODEX: default,
            AgentType.GROK: GrokCliDriver(),
            AgentType.ANTIGRAVITY: AntigravityCliDriver(),
            AgentType.SWINK_CODING: SwinkCodingCliDriver(),
        }

    def driver_for(self, agent_type: AgentType) -> CliDriver:
        return self._drivers[agent_type]


DEFAULT_CLI_DRIVERS = CliDriverRegistry()


__all__ = [
    "AntigravityCliDriver",
    "CliDriver",
    "CliDriverRegistry",
    "CliProviderOutput",
    "CliRunPreparation",
    "DEFAULT_CLI_DRIVERS",
    "DefaultCliDriver",
    "GrokCliDriver",
    "SwinkCodingCliDriver",
]
