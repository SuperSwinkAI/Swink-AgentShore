# Agent Manager - Functional Design

## Responsibility

The agent layer owns coding-agent lifecycle, dispatch, GitHub identity, and per-dispatch worktree placement:

- Instantiate CLI agents (`claude_code`, `codex`, `grok`, `antigravity`) as subprocess-backed handles. All supported agents are CLI-harnessed; raw HTTP/API agents are not supported.
- Track per-agent health, status, cost, token totals, dispatch count, task history, GitHub identity, model, model tier, and reasoning effort.
- Resolve and verify each agent's GitHub identity overlay once, then dispatch rendered AgentShore skill prompts and return raw output to the play executor.
- Place each dispatch in the correct git checkout (per-PR worktree, fresh branch worktree, or the main trunk checkout) without mutating the shared handle.
- Trip, cool down, and recover agents via per-agent circuit breakers when dispatches fail.

The agent manager does not decide which play should run. The RL selector chooses the play (see [../rl/DESIGN.md](../rl/DESIGN.md)), the parameter resolver chooses the concrete target, and the executor asks the manager to invoke the selected agent.

## Agent Types

| Type | Runtime | Notes |
|------|---------|-------|
| `claude_code` | CLI subprocess | Claude Code with JSON/stream output. Tiers: haiku / sonnet / opus, with reasoning effort low/medium/high passed via `--effort` (enum low/medium/high/xhigh/max). |
| `codex` | CLI subprocess | Codex CLI. Tiers: gpt-5.4-mini / gpt-5.4 / gpt-5.5 with reasoning effort low/medium/high via `-c model_reasoning_effort` (enum minimal/low/medium/high/xhigh). |
| `grok` | CLI subprocess | Grok CLI. `grok-4.5` is the only model (renamed from `grok-build`, which now collapses to it as a legacy alias); tiers differ solely by reasoning effort low/medium/high passed via `--effort` (enum low/medium/high/xhigh/max). Dispatched with `--no-memory --no-plan` (ephemeral single-turn runs). **Slowest agent by far** — measured time-to-first-byte is 30–70s (model/relay latency, not startup), so its first-byte watchdog deadline is widened to 240s (vs the 120s default) to avoid false launch-wedge kills. Prefer it for overflow over latency-sensitive plays. |
| `antigravity` | CLI subprocess | Antigravity CLI (binary `agy`). Exposes Google models (Gemini 3.5 Flash / Gemini 3.1 Pro variants) alongside non-Google backends (Claude Sonnet 4.6 (Thinking), Claude Opus 4.6 (Thinking), GPT-OSS 120B (Medium)), **plain-text output (no JSON format flag)**. Reasoning effort is baked into the model name, so there is no `--effort` flag and `reasoning_effort` is rejected at config-parse. Backend auth is `unprobeable`; the CLI emits no token usage, so token tracking is $0. Permission gates bypassed via `--dangerously-skip-permissions`. Replaced the deprecated Gemini CLI as Google's official AgentShore integration. |

Capabilities are declared statically per type in `capabilities.py` (`can_implement`, `can_review`, `can_test`, `can_create_pr`, `can_merge`, `can_create_issues`, plus `max_context` and per-token cost). Merge and issue creation are deliberately available to every type - they are AgentShore-mediated GitHub/repository operations, so scheduler availability can never strand an approved PR behind a disabled or saturated provider.

## Model Tiers

Each type exposes `small`, `medium`, and `large` tiers (`model_tiers.py`). `medium` is the default and the universal workhorse; `INSTANTIATE_AGENT` spawns in priority order medium -> small -> large. Explicit `model_tiers` config wins over the pinned defaults; legacy top-level `model`/`reasoning_effort` map onto the medium tier.

The flat model catalog (`model_catalog.py`) can expose additional opt-in models without making them defaults. Current examples: `claude-sonnet-5` and `claude-fable-5` for Claude, and `gpt-5.4-nano` for Codex (`gpt-5.5-pro` is deliberately excluded from the curated list — it 400s under ChatGPT-account auth, codex's default mode, which would permanently kill the agent via `INVALID_MODEL`; API-key users still reach it through the live model fetch), and explicit Gemini 3 model IDs for Antigravity. Grok is the exception: it is hard-pinned to `grok-4.5` (the only model the Grok CLI accepts for coding; the retired `grok-build` name collapses to it), so the catalog exposes no grok alternatives — grok tiers vary only by reasoning effort.

Per-play tier eligibility is a hard selection filter (`_selection.py`). Plays not listed accept any tier. Three design bands plus a universal exception:

| Band | Plays | Allowed tiers |
|------|-------|---------------|
| Universal (bootstrap) | `CLEANUP` | `small`, `medium`, `large` |
| Cheap mechanical / already-gated merge | `MERGE_PR` | `small`, `medium` |
| Coding & strategic | `ISSUE_PICKUP`, `UNBLOCK_PR`, `CODE_REVIEW`, `REFINE_TASK_BREAKDOWN`, `SYSTEMATIC_DEBUGGING`, `GROOM_BACKLOG`, `RECONCILE_STATE` | `medium`, `large` |
| Heavyweight validation / project-graph | `RUN_QA`, `WRITE_IMPLEMENTATION_PLAN`, `SEED_PROJECT`, `DESIGN_AUDIT`, `CALIBRATE_ALIGNMENT` | `large` |

`CLEANUP` keeps `small` because it is often the first play on a fresh session when only one tier has spawned; excluding any tier there would get it skipped for staffing. Otherwise `small` is kept off coding/strategic work (downstream cost risk) and `large` is reserved for trajectory-setting validation. The bands are deliberately broad so PPO learns tier affinity rather than the rules pre-committing it.

## Skill Dispatch

Skill-backed plays render a project-local skill prompt, write a play-specific context file, then dispatch to the chosen agent. The result parser extracts the last valid result-shaped JSON object from raw agent output. CLI permission gates are bypassed by default (autonomous orchestrator - agents can't pause for per-tool approval); a user who sets `extra_flags` opts out and manages flags themselves.

Skill templates source from `src/agentshore/skills/templates/` and install to `.agents/skills/<skill-name>/SKILL.md`:

`agentshore-calibrate-alignment`, `agentshore-cleanup`, `agentshore-code-review`, `agentshore-design-audit`, `agentshore-groom-backlog`, `agentshore-issue-pickup`, `agentshore-merge-pr`, `agentshore-prune`, `agentshore-reconcile-state`, `agentshore-refine-tasks`, `agentshore-run-qa`, `agentshore-seed-project`, `agentshore-systematic-debugging`, `agentshore-unblock-pr`, `agentshore-write-plan`.

## GitHub Identity

CLI agents can be bound to distinct GitHub identities (`identity.py`). A token resolves from, in priority order, `gh_token_env`, `gh_token_login` (`gh auth token -u <login>`), or `gh_token_keychain`; if all are unset the agent inherits ambient `gh` auth. The resolved env overlay (git authorship + `GH_TOKEN`) is built and repo-access-verified **once at `instantiate()`** and cached on the handle. On preflight failure the handle is marked `ERROR` (error class `auth`) and never registered live. Dispatch reuses the cached overlay rather than re-shelling `gh` on the hot path, adding only the per-dispatch `AGENTSHORE_PROJECT_PATH` (canonical main-repo root) so skills can anchor `MAIN_REPO` independent of the subprocess cwd. Identity drives the anti-confirmation selection rule below. See `docs/identity.md` for the provisioning reference.

## Backend Auth Probe

GitHub identity is only one of the two credentials a CLI agent needs; the other is its **backend session** with its model provider (e.g. the Codex CLI's cached `chatgpt.com` token), which carries its own TTL and expires independently of any `GH_TOKEN`. The identity preflight never sees it. `auth_probe.py` is the single source of truth for "is agent *type*'s backend auth currently valid?", shared by three call sites so a green badge on the desktop setup screen provably means the launch gate will pass: the CLI launch gate (`preflight_cli_agent_auth`), the desktop `session.start` `check_agent_auth` phase, and the desktop `agents.check_auth` RPC.

`probe_cli_auth(agent_type, env, *, binary, timeout)` runs a short, non-mutating auth-status subprocess under the agent's resolved identity env overlay and returns an `AuthProbeResult(agent_type, status, detail)` with `status` in `{ok, expired, timeout, error, unprobeable}`. Only `expired` blocks a launch — a transient probe hiccup (`timeout`/`error`) or an unsupported type (`unprobeable`) is surfaced but never strands an otherwise-fine session. The probe is intentionally conservative: only a type with a reliable, non-interactive status verb is probed, so it can never introduce a false-negative startup failure. Codex (`codex login status`) is the only real probe today; every other CLI type returns `unprobeable`. `probe_configured_cli_auth(cfg)` probes each enabled CLI type once (a backend token is shared across instances of a type).

## Selection Rules

`select_agent_for()` (`_selection.py`) draws from IDLE handles and applies hard filters first, then soft tiebreakers:

Hard, in order:
1. Required-id pin - resolver-chosen reviewer; if it raced out of IDLE the play is requeued rather than silently reassigned.
2. Required-type pin - for `INSTANTIATE_AGENT` and similar type-specific plays.
3. Anti-confirmation - `CODE_REVIEW` only: exclude any agent whose GitHub identity matches the PR author. When the author is unknown, all pass and the executor's identity check backstops.
4. Exclude list - drop agent types in `preferences.exclude[play]`.
5. Tier eligibility - drop tiers outside the play's allowed set.

If all candidates are eliminated, `AntiConfirmationViolation` is raised. Soft scoring (stable sort, lower = preferred): deprioritize circuit-broken agents, then prefer branch-exposure affinity, type affinity, cheaper tier, and least task history.

`RUN_QA` has no anti-confirmation rule - it validates merged trunk state, not a single author's branch, so any `can_test` agent is eligible.

## Worktree Placement

The `WorktreeManager` (owned by `AgentManager`) places every dispatch in the right git checkout. It returns a path; the dispatcher applies it via `cwd_override` and never mutates the handle's `working_dir`, so a single handle can run concurrent dispatches in different worktrees. Routing by play (full matrix in [../HLD.md](../HLD.md)):

| Play class | Placement |
|------------|-----------|
| PR-scoped (`CODE_REVIEW`, `UNBLOCK_PR`) | One lazily-created worktree per PR branch. |
| Branch-creating (`ISSUE_PICKUP`) | Fresh worktree, re-keyed by branch name after the play succeeds. |
| Trunk-scoped (QA, merge, seed, audits, calibrate, groom, planning, cleanup, reconcile) | The main checkout via a `TrunkAllocation` sentinel - no per-PR worktree. |

Worktrees default to `<repo>/.agentshore/worktrees/` (gitignored, same filesystem, never the repo's parent); `cfg.worktrees.root` centralizes them under `<root>/<repo-name>/worktrees/`. Trunk-*mutating* plays (`MERGE_PR`, `CLEANUP`, `RECONCILE_STATE`) additionally take an exclusive `trunk:main_repo` work claim so they can't race each other into a half-merged checkout; read-only trunk plays do not hold that lock (holding it starved merges - issue #17). A reaper sweeps orphan worktrees at session start and after PR close.

## Instantiate Agent

`INSTANTIATE_AGENT` expands capacity by `(agent_type, model_tier)`. A config can spawn when seed-project has completed, budget and cooldown allow it, live-agent counts are below caps, the config is enabled and not auth-blocked, and no idle same type/tier agent already exists. Busy same-config agents do not block another spawn.

## Health And Recovery

Each agent has a `CircuitBreaker` (CLOSED -> OPEN -> HALF_OPEN). Default: `3` failures in `300`s opens it; after a `60`s cooldown it goes HALF_OPEN, where a single further failure re-opens and a success closes it. Recovery backoff grows exponentially (capped) per attempt. Dispatch is refused while OPEN.

`attempt_recovery()` transitions an `ERROR` agent back to IDLE when the breaker allows - but skips config-class errors (`auth`, `invalid_model`) that re-attempting can't fix. `TAKE_BREAK` is the play that drives recovery of agents whose error class is `rate_limit` or `unknown` after the break interval. Timeouts carry a precise sub-class (wallclock / stream-idle / post-response) for sliced telemetry.

### Backend-auth suppression

A backend session can also expire *mid-run*, after a clean preflight. A live dispatch's stderr is sniffed for the backend-auth signatures (the same markers the probe uses, e.g. Codex's `failed to renew cache TTL`); on a hit the dispatch is aborted immediately with `ErrorClass.AUTH` instead of hanging to the full stream-idle timeout. The manager records that agent type on a grow-only `last_auth_failed_types` set; the state-builder mixin drains it each snapshot into the session's `auth_suppressed_agent_types`. The play-candidate analyzer then hard-masks every play that would dispatch to that type — including the `INSTANTIATE_AGENT` candidate that would spawn a fresh one — for the rest of the session, since a new backend token requires a new session to pick up. This stops one expired token from burning every subsequent dispatch to the timeout.

## Concurrency And Handoffs

Multiple agents and plays can be in flight simultaneously. Work claims prevent duplicate issue/PR assignment and serialize trunk mutation. Handoff records track context-transfer estimates when work moves between agents around `END_AGENT`. On process exit an `atexit` hook best-effort SIGTERMs any still-live agent subprocesses as a backstop to graceful shutdown.
