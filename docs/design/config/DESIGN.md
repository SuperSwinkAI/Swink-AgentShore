# Config — Functional Design

## Responsibility

AgentShore configuration is the single, validated source of truth for everything the orchestrator needs before the core loop starts: project discovery, agent fleet definitions, spawn pacing, budget enforcement, RL/reward tuning, session lifecycle, human-feedback policy, scope limits, worktree lifecycle, logging, UI, learnings, and skill installation.

All config is parsed from a single `agentshore.yaml` into deeply-immutable frozen dataclasses (`src/agentshore/config/models.py`). The source code is canonical for exact field names and defaults; this document records the user-facing contract and the design choices behind it.

Cross-references: [HLD](../HLD.md) lists this component; per-agent GitHub identities are documented in [`docs/identity.md`](../../identity.md).

## Design Choices

**Deep immutability.** Every config dataclass is `@dataclass(frozen=True)`. Collections are normalized in `__post_init__` to immutable forms — lists become tuples, dicts become `MappingProxyType`. The orchestrator holds one frozen `RuntimeConfig` instance for the life of a config generation, so no component can mutate shared state and there is no defensive copying. This is what makes atomic SIGHUP reload safe (see below).

**Parse-time validation over runtime guards.** YAML is validated and normalized once, at load, in `_parsers.py`. Invalid values raise `ConfigError` immediately rather than surfacing as obscure failures deep in the loop. Examples: budget floor enforcement, RL hyperparameter ranges, UI theme/log-level enums, `ssh_key_path` shell-metacharacter rejection (the path is interpolated into `GIT_SSH_COMMAND`), and agent-identity cross-validation.

**Defaults live in YAML, not just dataclasses.** The built-in default config is a YAML string parsed through the exact same path as a user file, so `agentshore start` runs with no config file and the no-file path can never diverge from the on-disk-file path. `agentshore init` writes that same YAML for the user to edit.

**Tolerant of legacy keys, loud about them.** Removed fields are kept in the raw TypedDicts so old YAML still parses, but the parser ignores them and emits a `DeprecationWarning`. This avoids hard-breaking existing project files on upgrade while steering users to the current schema.

## Files And Precedence

| Source | Scope | Precedence |
|--------|-------|------------|
| CLI flags | Current invocation | Highest |
| Explicit config path or `<project_root>/agentshore.yaml` | Project | Middle |
| Built-in default YAML | Package default | Lowest |

`load_config(path)` returns the built-in default config when `path` is `None` or the file is absent; otherwise it reads the file, requires a mapping at the root, and runs full validation. `generate_default_config()` writes the default YAML into a project.

Token pricing has its **own** file and precedence, separate from `agentshore.yaml` (see Pricing):

| Source | Scope | Precedence |
|--------|-------|------------|
| Global `pricing.yaml` (`GLOBAL_PRICING_PATH`) | All projects on the machine | Higher (deep-merged over the bundled default) |
| Bundled `agentshore/data/pricing.yaml` | Package default | Lower |

## Config Domains

`RuntimeConfig` aggregates the following sub-configs. Each is a frozen dataclass with its own defaults and parser.

| Domain | Purpose |
|--------|---------|
| `project` | Project path, freeform goals, and `target_branch` (PR base / merge target; `None` falls back to the repo's GitHub default branch). |
| `auto` | Toggles for autodetecting agents, GitHub, and API keys, and whether `init` generates config. |
| `intake` | Seed paths for the initial `seed_project` play and GitHub issue label include/exclude filters plus the AgentShore label prefix. |
| `budget` | Spend cap (see Budget). |
| `trusted_ids` | GitHub logins and a PR allow-list treated as trusted for review/merge gating. |
| `identities` | Named GitHub identities (git authorship + token source) bindable to CLI agents (see Identities). |
| `agents` | Per-agent-type fleet definitions, including per-tier spawn caps (see Agents / Spawn Limits). Token rates live in a separate file (see Pricing). |
| `pricebook` | Per-model token rates resolved from `pricing.yaml` (bundled + global override); not a YAML block under `agentshore.yaml` (see Pricing). |
| `play_pacing` | Standard post-run cooldown for heavyweight skill-backed plays (see Play Pacing). |
| `bootstrap` | First-play recipe tunable: `cleanup_threshold` open-issue count above which bootstrap queues `cleanup` instead of `seed_project`. |
| `fresh_start` | Context-reset thresholds (plays / context fraction / auto-trigger). |
| `agent_preferences` | Play→agent-type affinity and per-play exclusions. |
| `circuit_breaker` | Per-agent failure count / window / cooldown before tripping. |
| `health` | Agent health poll interval and stale-context play threshold. |
| `data_integrity` | SQLite corruption defense — quick-check canary, `VACUUM INTO` snapshot ring, and explicit WAL-checkpoint cadence. |
| `task_validation` | Per-task file/minute limits and an enforce toggle. |
| `rl` | PPO policy mode, learning hyperparameters, plus nested reward / PPO / stagnation / loop-detection blocks (see RL). |
| `session` | Max plays, timeout, auto-alignment cadence, archiving, and break duration. |
| `feedback` | When to request human feedback and the auto-stop / liveness backstops (see Feedback). |
| `scope` | Issue-inflation threshold, strict mode, and the mid-session `seed_project` issue ceiling. |
| `ui` | TUI theme (`dark`/`light`) and refresh rate. |
| `logging` | Level, file-logging toggle, log directory. |
| `timelapse` | Optional desktop dashboard timelapse capture (enabled / installed flags). |
| `learnings` | Session-learnings store size, decay, and prompt-injection knobs. |
| `skills` | Skill install-on-start toggle and install/context paths. |
| `worktrees` | Managed git-worktree reap TTL and optional centralized root. |

Top-level scalars: `agent_timeout` (global dispatch timeout fallback), `play_timeouts` (per-play-type overrides resolved by `effective_play_timeout`), `mode` (`solo`/`agent` run mode), and `socket` (IPC endpoint for agent mode).

## Agents

Each entry under `agents:` is an `AgentConfig`: binary, default model and reasoning effort, an approved-model allow-list, named `model_tiers` (small/medium/large, each with its own model + effort), context size, and stream/output/line-buffer limits. **There is no endpoint/API-base field** — AgentShore never configures model endpoints; for `swink_coding` (the only type fronting configurable endpoints) resolution is fully delegated to swink-coding's own `~/.swink-coding/config.toml` (decision record: `docs/design/agents/DESIGN.md`, #327). **Token rates are no longer part of `AgentConfig`** — they live in the external pricing table (see Pricing); `max_context` defaults still come from that table's per-agent-type entry. The line buffer defaults to 4 MB because CLI agents emit stream-json result lines that exceed asyncio's 64 KB default; the stream-idle timeout defaults to 30 minutes so legitimate long-think windows survive while genuinely hung agents are still detected. Known agent types (`claude_code`, `codex`, `grok`, `antigravity`, `swink_coding`) carry built-in context defaults so a minimal entry is enough. `reasoning_effort` is not configurable for `antigravity` (effort is baked into the model name, so the CLI exposes no effort flag and the parser rejects one — the same way it does for `grok`) nor for `swink_coding` (the tier alias is the model; effort, like the concrete model, is swink-coding's business). The reserved `fresh_start` and `preferences` keys under `agents:` are parsed into their own configs, not as agents.

## Pricing

Per-model token rates are factored out of `agentshore.yaml` into a dedicated YAML table so prices — the values most likely to change as providers reprice — have a **single touchpoint** that can be edited without a code change or rebuild. The canonical default ships in the wheel at `agentshore/data/pricing.yaml`; a global file at `GLOBAL_PRICING_PATH` (`platformdirs` user-config dir, e.g. `~/Library/Application Support/agentshore/pricing.yaml`) **deep-merges** over it, so an operator lists only the models they reprice. `load_pricebook()` reads bundled + global on every call and builds a frozen `PriceBook` held on `RuntimeConfig.pricebook`.

The table has three tiers plus the cache multipliers (the read/write factors applied to a model that omits an explicit cached / cache-write rate):

- `models:` — per-model rates, keyed by the `model:` strings used in `model_tiers` (highest precedence).
- `agent_defaults:` — per-agent-type fallback, used when the resolved model id is not listed.
- `default:` — last-resort global fallback.

At cost time the Agent Manager resolves `pricebook.resolve(agent_type, model)` (model id → agent-type default → global default) and hands `estimate_cost` a `PricingQuote` (the resolved `AgentPricing` plus the cache multipliers). A named model that falls past the per-model tier is logged once per `(agent_type, model)` so an unpriced model surfaces without crashing the play or silently mis-billing. The loader validates rates (positive, required input/output present) and shape, raising `ConfigError` exactly like a bad `agentshore.yaml`. Agent-type defaults mirror the historical built-in rates, so billing is unchanged for any model not yet enumerated under `models:`.

## Identities

`identities:` defines named GitHub identities, each supplying `git_user_name`/`git_user_email` and at most one token source (`gh_token_env`, `gh_token_login`, or `gh_token_keychain`); all unset means the agent inherits ambient `gh` auth. Keys are canonicalized with GitHub's case-insensitive login rules (duplicates rejected). An agent binds one via its `identity:` field. API-only agents (`api_` prefix) reject `identity:` at parse time because `gh` is never invoked for them, and any agent referencing an unknown identity is a `ConfigError`.

## Spawn Limits

The `INSTANTIATE_AGENT` cap is **per `(agent_type, model_tier)` cell**, set by the `max` field on each entry under an agent's `model_tiers:` (see Agents):

| Field | Default | Range | Meaning |
|-------|---------|-------|---------|
| `model_tiers.<tier>.max` | `1` | `1`–`20` (silently clamped) | Max live agents for that one `(agent_type, model_tier)`. A tier with `enabled: false` is never spawned regardless of `max`. |

The former global `agent_spawn` block (`max_per_config`, `cooldown_plays`, and the older `max_total`) was removed in favor of per-tier `max`. A legacy `agent_spawn.max_per_config` is migrated automatically — it fans out to every explicitly-configured tier that omits its own `max`, and a `DeprecationWarning` is emitted (remove the `agent_spawn` block to silence it). Note: agents relying entirely on default tiers (no `model_tiers:` block) fall back to `max: 1`, not the legacy value.

Per-(type, tier) gating is sufficient because PPO cannot starve one cell by concentrating in another, so budget enforcement is the practical fleet ceiling. A type/tier is spawnable only when enabled, within that tier's `max`, not blocked by auth/model errors, and no idle same type/tier agent already exists; busy agents do not block another same-config spawn. A single in-flight instantiate dispatch holds the next one until it lands, purely to prevent same-tick overshoot.

## Play Pacing

`play_pacing` controls shared post-run cooldowns for heavyweight skill-backed plays. The standard cooldown applies to `cleanup`, `run_qa`, `design_audit`, `groom_backlog`, `calibrate_alignment`, and `prune`; each play still owns its other gates such as warmup, beads initialization, capability, in-flight, and debt thresholds.

| Field | Default | Meaning |
|-------|---------|---------|
| `standard_cooldown_plays` | `42` | Completed plays required before a standard-cooldown play can run again. |

## Budget

A session is bounded by two **independent soft caps** under the `budget` block — dollars and wall-clock time. Either cap reaching its reserve triggers the same graceful drain (stop assigning new plays, let in-flight agents finish); the cap itself is the hard-stop backstop.

**Dollar cap.** `budget.enabled: false` disables the cap even when `budget.total` is set. When enabled, `budget.total` must be at least `MIN_ENABLED_BUDGET_USD` (validated at load). Drain begins at a `BUDGET_DRAIN_RESERVE_USD` ($5) reserve. `warning_threshold` is the remaining-fraction trigger and must be in `[0, 1]`.

**Time cap.** `budget.time_enabled: false` disables the wall-clock cap. When enabled, `budget.time_total_minutes` must be in `[MIN_TIME_BUDGET_MINUTES, MAX_TIME_BUDGET_MINUTES]` = 60–4320 (1h–72h). Drain begins `TIME_BUDGET_DRAIN_RESERVE_MINUTES` (20 min) before the deadline; the deadline hard-stop emits a `time_budget` reason. This is a deterministic backstop only — it is **not** in the PPO observation vector (observation version unchanged). The former `session.timeout_minutes` field is migrated onto this one (a single wall-clock enforcement path).

**CLI.** `--budget <$>` and `--time <DURATION>` (e.g. `24h`, `90m`, bare minutes) set each cap; `--unlimited` disables both. A "naked" `agentshore start` uses `agentshore.yaml` if a budget is configured there; on a fresh/unconfigured project it falls back to the $200 + 24h safety defaults, and naming one dimension on the CLI suppresses the other's bare default.

**Live mid-session control (Feature B).** Both caps can change on a *running* session without a restart, applied within one tick and persisted back to `agentshore.yaml` so they survive one. The shared core lives on the orchestrator: `set_budget` (absolute-set, per-dimension enable, incl. "unlimited") and `add_budget` (additive top-up / time extension); `effective_budget_caps()` resolves live overrides over the frozen `_cfg.budget` as the single source of truth (config stays immutable — overrides shadow it). Raising a cap that moves the session back outside its reserve resumes a budget pause or reverses an in-progress reserve drain. Two thin transports reach the same core: the **sidecar JSON-RPC** `session.set_budget` / `session.get_budget` (desktop "Adjust Budget…" dialog, absolute-set) and the **NDJSON line-IPC** `add_budget` command behind `agentshore add-budget --budget N --time DUR` (additive). Bounds are validated client- and server-side (the same $20 / 1h–72h rules as load). See `docs/design/ipc/DESIGN.md` for the wire formats.

## RL

`rl` holds the policy mode plus learning hyperparameters and three nested blocks. `policy_mode` is `learning` (PPO updates on, sampled selection) or `audit-replay` (learning off, greedy selection); the legacy boolean `rl.deterministic` is still accepted but deprecated and may not conflict with an explicit `policy_mode`. Learning rate, gamma, and entropy coefficient are range-validated. Nested blocks: `reward` (per-signal weights and shaping bonuses/penalties for alignment, throughput, cost, time, completion, anti-confirmation, progress/QA/merge bonuses, loop/inflation penalties, and velocity), `ppo` (clip epsilon, GAE lambda, epochs, batch, loss/grad clipping), `stagnation` (whole-minute all-idle escalation thresholds), and `loop_detection` (repeat-play warn/switch/escalate counts and the fleet-idle-persistent threshold).

## Feedback And Liveness Backstops

`feedback` governs when the loop pauses to request human input (stagnation, budget exhaustion, loop escalation, ambiguous intake) and the safety timers that prevent a paused or frozen loop from wedging indefinitely:

- `unanswered_timeout_seconds` (default 120): auto-stop the session if a feedback prompt goes unanswered this long — a loop that is alive but waiting on a human.
- `loop_liveness_timeout_seconds` (default 600): an independent watchdog, off the loop's critical path, force-drains the session if the loop's heartbeat stops advancing — a loop that has stopped iterating entirely.

Either timer set to `None` disables that backstop.

## Hot Reload

The orchestrator reloads `agentshore.yaml` on SIGHUP. Because config is a single frozen instance, reload is atomic: the new file is parsed and validated in full, and only on success is the active `RuntimeConfig` instance swapped wholesale. An invalid reload is rejected and the previous instance stays active, so a bad edit can never partially apply or crash a running session.

Reload rebuilds the `pricebook` too (it re-runs `load_pricebook()`, re-reading the bundled + global `pricing.yaml`), so editing the global pricing file then sending SIGHUP reprices the **next** dispatch with no restart — in-flight agents finish at the old rate. A malformed pricing file makes the reload fail validation and is rejected like any other bad edit.
