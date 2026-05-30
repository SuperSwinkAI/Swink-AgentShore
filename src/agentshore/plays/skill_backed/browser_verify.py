"""BrowserVerificationPlay — run agentshore-browser-verify on a branch."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import TYPE_CHECKING

from agentshore.plays.dispatch import (
    params_to_json_safe_dict,
    play_context_relative_path,
    render_skill_prompt,
    serialize_state_for_skill,
    write_play_context,
)
from agentshore.plays.skill_backed.base import SkillBackedPlay, _looks_like_auth_failure
from agentshore.result_parser import parse_skill_result
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import ActivePlay, JsonArtifact, PlayOutcome, PlayType

if TYPE_CHECKING:
    from agentshore.agents.handle import AgentInvocationResult
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState


class BrowserVerificationPlay(SkillBackedPlay):
    """Perform browser smoke-tests against a deployed branch.

    Precondition: cfg.browser.enabled must be True.
    """

    def __init__(self, *, browser_enabled: bool = True) -> None:
        self._browser_enabled = browser_enabled

    @property
    def play_type(self) -> PlayType:
        return PlayType.BROWSER_VERIFICATION

    @property
    def skill_name(self) -> str:
        return "agentshore-browser-verify"

    @property
    def capability(self) -> str | None:
        return "can_test"

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        if not self._browser_enabled:
            return [
                MaskReason(
                    text="browser verification disabled in config",
                    classification=MaskClassification.HARD,
                    source=MaskSource.CONFIG,
                )
            ]
        return []

    async def _emit_phase(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
        phase: str,
    ) -> None:
        if ctx.state_provider is None:
            return
        active = state.active_play
        if active is not None:
            state.active_play = dataclasses.replace(active, phase=phase, play_id=ctx.play_id)
        else:
            state.active_play = ActivePlay(
                play_type=self.play_type,
                agent_id=params.agent_id,
                started_at=str(params.extras.get("started_at") or ""),
                play_id=ctx.play_id,
                issue_number=params.issue_number,
                pr_number=params.pr_number,
                branch=params.branch,
                phase=phase,
            )
        await ctx.state_provider.on_state_update(state)

    async def _dispatch_and_parse(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> tuple[AgentInvocationResult, bool, list[JsonArtifact], str | None]:
        agent_id = params.agent_id
        if agent_id is None:
            return (self._no_agent_invocation(), False, [], "agent_id not resolved before execute")

        context_relative_path = play_context_relative_path(ctx.play_id, session_id=ctx.session_id)
        payload = serialize_state_for_skill(
            session_id=ctx.session_id,
            play_id=ctx.play_id,
            play_type=self.play_type,
            skill_name=self.skill_name,
            params=params,
            open_issues=state.open_issues,
            budget_enabled=state.budget.enabled if state.budget else ctx.cfg.budget.enabled,
            budget_total=state.budget.total_budget if state.budget else ctx.cfg.budget.total,
            budget_spent=state.budget.spent if state.budget else 0.0,
            learnings_count=0,
            pull_requests=state.pull_requests,
            top_learnings=[],
            mode=ctx.cfg.mode,
            assigned_github_identity=None,
            target_branch=ctx.cfg.project.target_branch,
            project_path=str(ctx.project_path.resolve()),
            extra={"review_patterns": []},
        )
        await asyncio.to_thread(
            write_play_context,
            ctx.project_path,
            payload,
            context_relative_path=context_relative_path,
        )
        prompt = await render_skill_prompt(
            self.skill_name,
            params,
            project_path=ctx.project_path,
            context_path=context_relative_path,
        )
        claim_group_id_raw = params.extras.get("claim_group_id")
        if isinstance(claim_group_id_raw, str) and claim_group_id_raw:
            await ctx.store.save_dispatch_replay(
                session_id=ctx.session_id,
                claim_group_id=claim_group_id_raw,
                play_id=ctx.play_id,
                skill_name=self.skill_name,
                params_json=json.dumps(params_to_json_safe_dict(params)),
                prompt=prompt,
                branch=params.branch,
            )
        from agentshore.plays.skill_backed.base import _worktree_cwd_override

        invocation = await ctx.manager.dispatch(
            agent_id,
            prompt,
            capability=self.capability,
            play_type=self.play_type.value,
            cwd_override=_worktree_cwd_override(params),
        )
        skill_result = parse_skill_result(invocation.raw_output)
        self._last_skill_result = skill_result
        if not skill_result.success and _looks_like_auth_failure(skill_result.error):
            await ctx.manager.mark_agent_error(
                agent_id,
                "auth",
                skill_result.error or "skill reported GitHub authentication failure",
            )
        return invocation, skill_result.success, skill_result.artifacts, skill_result.error

    def _no_agent_invocation(self) -> AgentInvocationResult:
        from agentshore.agents.handle import AgentInvocationResult

        return AgentInvocationResult(
            raw_output="",
            tokens_in=0,
            tokens_out=0,
            dollar_cost=0.0,
            duration_ms=0,
            exit_code=1,
        )

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        phases = [
            "launching browser",
            "navigating",
            "capturing screenshot",
            "verifying",
            "closing",
        ]
        last_phase: str | None = None
        invocation = self._no_agent_invocation()
        success = False
        artifacts: list[JsonArtifact] = []
        error: str | None = None
        try:
            for phase in phases[:-1]:
                await self._emit_phase(state, params, ctx=ctx, phase=phase)
                last_phase = phase
            invocation, success, artifacts, error = await self._dispatch_and_parse(
                state, params, ctx=ctx
            )
            if success:
                await self._emit_phase(state, params, ctx=ctx, phase=phases[-1])
                last_phase = phases[-1]
            return PlayOutcome(
                play_type=self.play_type,
                agent_id=params.agent_id,
                success=success,
                partial=False,
                duration_seconds=invocation.duration_ms / 1000.0,
                token_cost=invocation.tokens_in + invocation.tokens_out,
                dollar_cost=invocation.dollar_cost,
                artifacts=artifacts,
                alignment_delta=0.0,
                error=error,
            )
        except Exception as exc:
            if last_phase is not None:
                await self._emit_phase(state, params, ctx=ctx, phase=f"{last_phase} — FAILED")
            return PlayOutcome.failed(self.play_type, str(exc), agent_id=params.agent_id)
        finally:
            if not success and last_phase is not None and error is not None:
                await self._emit_phase(state, params, ctx=ctx, phase=f"{last_phase} — FAILED")
