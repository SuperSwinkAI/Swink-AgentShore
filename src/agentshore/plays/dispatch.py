"""Low-level skill dispatch primitives: prompt rendering and context writing."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from agentshore.agents.context_writer import write_context_file
from agentshore.config.models import RunMode
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayParams
    from agentshore.state import IssueSnapshot, PullRequestSnapshot

_logger = structlog.get_logger(__name__)


# Plays that need the full open_issues list — they reason across the whole
# issue landscape (selection, prioritization, scoring) rather than acting on one
# specific issue. Every other play either targets a single issue (filtered by
# params.issue_number) or doesn't reference issues at all.
_FULL_ISSUES_PLAYS: frozenset[PlayType] = frozenset(
    {
        PlayType.REFINE_TASK_BREAKDOWN,
        PlayType.ISSUE_PICKUP,
        PlayType.GROOM_BACKLOG,
        PlayType.SEED_PROJECT,
        PlayType.DESIGN_AUDIT,
        PlayType.CALIBRATE_ALIGNMENT,
    }
)

# Plays that need the full pull_requests list. Most PR-targeted plays are
# scoped to a single PR via params.pr_number; only cross-PR analysis warrants
# the full list.
_FULL_PRS_PLAYS: frozenset[PlayType] = frozenset(
    {
        PlayType.CALIBRATE_ALIGNMENT,
    }
)


# ---------------------------------------------------------------------------
# Skill specs — single source of truth for play → skill name + arg fields
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SkillSpec:
    name: str
    args: list[str]  # ordered PlayParams field names passed as positional args


_SKILL_SPECS: dict[PlayType, _SkillSpec] = {
    PlayType.UNBLOCK_PR: _SkillSpec("agentshore-unblock-pr", ["pr_number"]),
    PlayType.WRITE_IMPLEMENTATION_PLAN: _SkillSpec("agentshore-write-plan", ["issue_number"]),
    PlayType.SYSTEMATIC_DEBUGGING: _SkillSpec(
        "agentshore-systematic-debugging", ["issue_number", "branch"]
    ),
    PlayType.ISSUE_PICKUP: _SkillSpec("agentshore-issue-pickup", ["issue_number"]),
    PlayType.CODE_REVIEW: _SkillSpec("agentshore-code-review", ["pr_number"]),
    PlayType.RUN_QA: _SkillSpec("agentshore-run-qa", ["branch"]),
    PlayType.MERGE_PR: _SkillSpec("agentshore-merge-pr", ["pr_number"]),
    PlayType.REFINE_TASK_BREAKDOWN: _SkillSpec("agentshore-refine-tasks", []),
    PlayType.CLEANUP: _SkillSpec("agentshore-cleanup", []),
    PlayType.GROOM_BACKLOG: _SkillSpec("agentshore-groom-backlog", []),
    PlayType.SEED_PROJECT: _SkillSpec("agentshore-seed-project", ["seed_path"]),
    PlayType.DESIGN_AUDIT: _SkillSpec("agentshore-design-audit", []),
    PlayType.CALIBRATE_ALIGNMENT: _SkillSpec("agentshore-calibrate-alignment", []),
    PlayType.RECONCILE_STATE: _SkillSpec("agentshore-reconcile-state", []),
}

# Derived lookups — add new plays to _SKILL_SPECS only, these stay in sync.
PLAY_SKILL_MAP: dict[PlayType, str] = {pt: spec.name for pt, spec in _SKILL_SPECS.items()}
_SKILL_ARGS: dict[str, list[str]] = {spec.name: spec.args for spec in _SKILL_SPECS.values()}

DEFAULT_CONTEXT_RELATIVE_PATH = ".agentshore/context.json"

_CONTEXT_DISCIPLINE_TEMPLATE = """## AgentShore Context Discipline
{cwd_block}
AgentShore writes `{context_path}` immediately before this play. Read that file first.
The legacy `.agentshore/context.json` file is only a latest-context/debug copy and may
belong to another concurrent play; use it only if `{context_path}` is missing.
Use its `learnings` field as the compact learning-history input. For this skill, do not
read `.agentshore/learnings.json`; if the compact context is missing, continue without
learning-history context.

For GitHub mutations, keep the injected `GH_TOKEN` and `GITHUB_TOKEN` environment
variables intact. Do not unset them, do not fall back to GitHub connector paths for PR
creation, and use the `gh` CLI for repository mutations. Before mutating GitHub, run
`gh api user --jq .login`; if `{context_path}` contains `assigned_github_identity`, the
login must match that value after lowercasing/casefolding both strings. GitHub login
casing is not significant.

Never pipe test or validation output through `tail`, `head`, or any buffering filter.
If the command exceeds the Bash timeout it gets promoted to a background task whose
pipe keeps the process tree alive indefinitely, causing the play to time out.  Use
compact output flags instead (`-q --tb=line` for pytest, `--short` for mypy).

This is a single, non-interactive turn. Nothing will "wake you up", "re-invoke you on
completion", or send a "task notification" — there is no callback and no scheduler
watching for you. Never end your turn to wait for user input, a background job, a build
or test run, a package-manager lock, CI, or any notification: run every command in the
foreground to completion within this turn, or kill it and proceed with what you have.
The closing reminder below restates the result block you must emit before you stop.
"""

# Appended to the VERY END of every rendered skill prompt (after the SKILL.md body), so
# the completion contract sits in the most attention-privileged position — closest to
# where the agent acts. Bookends the start-of-prompt discipline: terminal instructions
# (emit the result block; do not pause to wait) are recency-sensitive, and in ``-p`` mode
# the start of the prompt is far in the past by the time a long tool-use trajectory ends.
_COMPLETION_CONTRACT_TEMPLATE = """## Before you stop — required

Emit the fenced JSON result block defined above as the final thing you do. This was a
single turn with no callback: do not pause to wait for anything still running — finish
it or kill it, then emit the block. Omitting the block records the play as failed
(`no valid result block`) and discards everything you did, including any PR you opened.
"""

# Interpolated into ``_CONTEXT_DISCIPLINE_TEMPLATE`` as ``{cwd_block}`` when the
# dispatcher allocated an isolated worktree for this play. Tells the agent where
# it is — the single biggest source of "file does not exist" / "cannot change to
# worktree" retries was agents guessing absolute/stale paths because the preamble
# never named their cwd. The reclaimed-mid-play instruction matches the
# orchestrator's recoverable-crash mapping (a reaped cwd surfaces as a recoverable
# AgentProcessCrashed, not a hard failure).
_CWD_DISCIPLINE_TEMPLATE = """
**Your working directory.** You are running inside an isolated git worktree at
`{dispatch_cwd}`. That path is your current working directory — every relative path
resolves from there. Do not `cd` into another checkout, and do not use absolute paths
from a different worktree or the main repo. If any command reports that the worktree is
missing or the working directory no longer exists, stop immediately and emit
`worktree reclaimed mid-play` in your result — do not retry, recreate the directory, or
run `git worktree add`.
"""


# ---------------------------------------------------------------------------
# Prompt renderer
# ---------------------------------------------------------------------------


async def render_skill_prompt(
    skill_name: str,
    params: PlayParams,
    project_path: Path | None = None,
    context_path: str = DEFAULT_CONTEXT_RELATIVE_PATH,
    dispatch_cwd: str | Path | None = None,
) -> str:
    """Render the full skill prompt for *skill_name* with *params*.

    Always returns the embedded SKILL.md body (with arguments and AgentShore
    context discipline prepended). If the skill file is missing under
    ``<project>/.agents/skills/<skill_name>/SKILL.md``, this function
    self-heals by reinstalling the named skill from the bundled package
    templates and re-reading.

    Raises ``FileNotFoundError`` if the bundled template is also unavailable
    (which indicates a packaging bug). The previous slash-command fallback
    has been removed because dispatched agents in ``-p`` mode often failed
    to route literal ``/agentshore-*`` strings.
    """
    arg_fields = _SKILL_ARGS.get(skill_name, [])
    args: list[str] = []
    for field_name in arg_fields:
        value = getattr(params, field_name, None)
        if value is not None:
            args.append(_quote_arg(str(value)))

    arg_str = " ".join(args) if args else ""

    if project_path is None:
        raise FileNotFoundError(
            f"skill template not installed and bundled template missing: {skill_name}"
        )

    skill_content = await _read_skill_md(project_path, skill_name)
    if skill_content is None:
        # Auto-reinstall from bundled package templates and re-attempt.
        from agentshore.skills import install_skills

        await asyncio.to_thread(install_skills, Path(project_path), only=[skill_name], force=False)
        skill_content = await _read_skill_md(project_path, skill_name)
        if skill_content is None:
            raise FileNotFoundError(
                f"skill template not installed and bundled template missing: {skill_name}"
            )
        _logger.info("skill_template_auto_reinstalled", skill=skill_name)

    skill_content = _strip_full_learnings_reads(skill_content)
    header = f"$ARGUMENTS: {arg_str}" if arg_str else "$ARGUMENTS: (none)"
    cwd_block = (
        _CWD_DISCIPLINE_TEMPLATE.format(dispatch_cwd=dispatch_cwd)
        if dispatch_cwd is not None
        else ""
    )
    discipline = _CONTEXT_DISCIPLINE_TEMPLATE.format(context_path=context_path, cwd_block=cwd_block)
    return f"{header}\n\n{discipline}\n\n{skill_content}\n\n{_COMPLETION_CONTRACT_TEMPLATE}"


async def _read_skill_md(project_path: Path, skill_name: str) -> str | None:
    """Return the (frontmatter-stripped) SKILL.md body for *skill_name* or None.

    Looks under ``.agents/skills/<skill>/SKILL.md``.
    """
    skill_file = Path(project_path) / ".agents" / "skills" / skill_name / "SKILL.md"
    if not skill_file.exists():
        return None
    text = await asyncio.to_thread(skill_file.read_text, encoding="utf-8")
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :].strip()
    return text


def _strip_full_learnings_reads(skill_content: str) -> str:
    lines = [
        line for line in skill_content.splitlines() if ".agentshore/learnings.json" not in line
    ]
    return "\n".join(lines).strip()


def _quote_arg(value: str) -> str:
    """Shell-quote an argument only when it contains spaces or special chars."""
    if " " in value or any(c in value for c in "\"'\\"):
        return json.dumps(value)
    return value


# ---------------------------------------------------------------------------
# Context file writer
# ---------------------------------------------------------------------------


def play_context_relative_path(play_id: int, *, session_id: str | None = None) -> str:
    """Return the per-dispatch context path, relative to project root."""
    if session_id:
        safe_session_id = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in session_id
        )
        return f".agentshore/contexts/{safe_session_id}/play-{play_id}.json"
    return f".agentshore/contexts/play-{play_id}.json"


def _scope_issues(
    play_type: PlayType,
    params: PlayParams,
    open_issues: list[IssueSnapshot],
) -> list[IssueSnapshot]:
    """Return only the open issues this play needs in its context.json."""
    if params.issue_number is not None:
        return [i for i in open_issues if i.issue_number == params.issue_number]
    if play_type in _FULL_ISSUES_PLAYS:
        return list(open_issues)
    return []


def _scope_prs(
    play_type: PlayType,
    params: PlayParams,
    pull_requests: list[PullRequestSnapshot],
) -> list[PullRequestSnapshot]:
    """Return only the pull requests this play needs in its context.json."""
    if params.pr_number is not None:
        return [pr for pr in pull_requests if pr.pr_number == params.pr_number]
    if play_type in _FULL_PRS_PLAYS:
        return list(pull_requests)
    return []


def _json_safe_extras(extras: dict[str, object]) -> dict[str, object]:
    """Return a copy of ``params.extras`` safe to ``json.dump``.

    Issue #565 moved the worktree allocation dataclass off ``extras`` onto
    a private ``PlayParams._runtime_allocation`` field, so the original
    motivating leak (TrunkAllocation/Path/PlayType) can no longer reach
    this helper. The conversion logic stays as defense-in-depth: ``extras``
    is still typed ``dict[str, object]``, so anything else stamped here in
    the future is normalised before ``json.dump``.

    Conversions: ``Path`` → str, ``Enum`` → ``.value``, dataclasses → dict
    via field walk, dict/list/tuple → recursed. Other values pass through;
    if ``json.dump`` later fails on something exotic, it raises at the
    actual write site (where the offending key is visible in the
    traceback), which is more debuggable than masking with ``str()``.
    """

    def _convert(value: object) -> object:
        if isinstance(value, Path):
            # as_posix(), not str(): this dict is a JSON wire/replay format, so
            # serialize paths deterministically with forward slashes rather than
            # the host's native separator (backslash on Windows). Windows tools
            # accept forward-slash paths, and the runtime cwd uses the private
            # _runtime_allocation Path, never this serialized copy.
            return value.as_posix()
        if isinstance(value, Enum):
            return value.value
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {f.name: _convert(getattr(value, f.name)) for f in dataclasses.fields(value)}
        if isinstance(value, dict):
            return {str(k): _convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_convert(item) for item in value]
        return value

    return {str(k): _convert(v) for k, v in extras.items()}


def params_to_json_safe_dict(params: PlayParams) -> dict[str, object]:
    """Return a JSON-safe dict for ``PlayParams``.

    Used by skill-backed plays that persist ``params_json`` to the dispatch
    replay table. Deliberately omits ``_runtime_allocation`` (issue #565):
    the allocation handle is runtime-only and must never cross the JSON
    boundary. Sanitises ``extras`` via ``_json_safe_extras`` for any other
    non-JSON-safe values future code might stamp there.
    """
    return {
        "agent_id": params.agent_id,
        "issue_number": params.issue_number,
        "pr_number": params.pr_number,
        "branch": params.branch,
        "num_commits": params.num_commits,
        "url": params.url,
        "seed_path": params.seed_path,
        "scope": params.scope,
        "target_agent_type": params.target_agent_type,
        "target_model_tier": params.target_model_tier,
        "source_agent_id": params.source_agent_id,
        "target_agent_id": params.target_agent_id,
        "reason": params.reason,
        "bypass_preconditions": params.bypass_preconditions,
        "extras": _json_safe_extras(params.extras),
    }


def serialize_state_for_skill(
    *,
    session_id: str,
    play_id: int,
    play_type: PlayType,
    skill_name: str | None,
    params: PlayParams,
    open_issues: list[IssueSnapshot],
    budget_enabled: bool,
    budget_total: float,
    budget_spent: float,
    learnings_count: int,
    pull_requests: list[PullRequestSnapshot] | None = None,
    top_learnings: list[dict[str, object]] | None = None,
    mode: RunMode | str = RunMode.SOLO,
    assigned_github_identity: str | None = None,
    target_branch: str | None = None,
    project_path: str | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build the payload written to ``.agentshore/context.json`` before dispatch.

    open_issues / pull_requests are scoped to what the play actually needs:
    a single record when params target one, the full list for cross-cutting
    plays, otherwise empty. This keeps context.json compact at scale.

    ``target_branch`` is the configured ``project.target_branch`` (``None`` when
    unset). Skill prompts read it from ``context.json`` to scope PR base and
    merge-target branches per desktop-53m0; templates fall back to the repo
    default when the field is null.
    """
    scoped_issues = _scope_issues(play_type, params, open_issues)
    scoped_prs = _scope_prs(play_type, params, pull_requests or [])
    return {
        "schema_version": 1,
        "session_id": session_id,
        "mode": mode,
        "current_play": play_type.value,
        "skill_name": skill_name,
        "play_id": play_id,
        "assigned_github_identity": assigned_github_identity,
        "target_branch": target_branch,
        "params": {
            "agent_id": params.agent_id,
            "issue_number": params.issue_number,
            "pr_number": params.pr_number,
            "branch": params.branch,
            "num_commits": params.num_commits,
            "url": params.url,
            "seed_path": params.seed_path,
            "scope": params.scope,
            "reason": params.reason,
            "extras": _json_safe_extras(params.extras),
        },
        "open_issues": [
            {
                "issue_number": i.issue_number,
                "title": i.title,
                "state": i.state,
                "priority": i.priority,
                "labels": i.labels,
            }
            for i in scoped_issues
        ],
        "pull_requests": [
            {
                "pr_number": pr.pr_number,
                "title": pr.title,
                "state": pr.state,
                "branch": pr.branch,
                "issue_number": pr.issue_number,
                "linked_issue_numbers": list(pr.linked_issue_numbers),
                "labels": pr.labels,
                "review_decision": pr.review_decision,
                "status_check_summary": pr.status_check_summary,
                "is_draft": pr.is_draft,
                "blocked": pr.blocked,
                "blocked_reasons": pr.blocked_reasons,
                "url": pr.url,
                "github_author": pr.github_author,
                "head_sha": pr.head_sha,
                "mergeable": pr.mergeable,
                "last_reviewed_sha": pr.last_reviewed_sha,
                "last_review_status": pr.last_review_status,
            }
            for pr in scoped_prs
        ],
        "budget": {
            "enabled": budget_enabled,
            "total": budget_total,
            "spent": budget_spent,
            "remaining": max(0.0, budget_total - budget_spent) if budget_enabled else None,
        },
        "learnings_count": learnings_count,
        "learnings": top_learnings or [],
        "project_path": project_path,
        **(extra or {}),
    }


def write_play_context(
    project_path: Path,
    payload: dict[str, object],
    *,
    context_relative_path: str = DEFAULT_CONTEXT_RELATIVE_PATH,
) -> int:
    """Atomically write *payload* to a context file under *project_path*.

    Returns the byte size of the written context file so callers can emit
    telemetry. Delegates to ``agents.context_writer.write_context_file``
    which uses a temp-file + os.replace for crash safety.

    ``.agentshore/context.json`` remains a best-effort latest-context copy for
    older tooling and debugging, but concurrent dispatches must read the
    per-play file passed to their prompt.
    """
    payload_to_write = {**payload, "context_file": context_relative_path}
    context_path = project_path / context_relative_path
    bytes_written = write_context_file(context_path, payload_to_write)
    latest_path = project_path / DEFAULT_CONTEXT_RELATIVE_PATH
    if context_path != latest_path:
        write_context_file(latest_path, payload_to_write)
    play_type = payload.get("current_play")
    skill_name = payload.get("skill_name")
    raw_issues = payload.get("open_issues")
    open_issues_count = len(raw_issues) if isinstance(raw_issues, list) else 0
    raw_prs = payload.get("pull_requests")
    pull_requests_count = len(raw_prs) if isinstance(raw_prs, list) else 0
    _logger.info(
        "context_json_written",
        play_type=play_type,
        skill_name=skill_name,
        path=context_relative_path,
        bytes=bytes_written,
        open_issues=open_issues_count,
        pull_requests=pull_requests_count,
    )
    return bytes_written
