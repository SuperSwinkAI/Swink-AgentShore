# Preferences Menu & `agentshore preferences` — Decisions

## Overview

Add a global, machine-level **Preferences** system to AgentShore: a Desktop settings
pane plus an `agentshore preferences` CLI, both backed by a single user-level
`preferences.yaml`. The headline capability is letting a user disable non-delivery-critical
plays (QA, cleanup, prune, design-audit, groom-backlog). Secondary scope migrates a curated
set of genuinely machine-global config fields (agent/drain timeouts, log level, UI theme)
out of the per-project `agentshore.yaml` and into this global file. All preferences apply
mid-run. The per-project **agent roster** (enabled/models/max) is explicitly *out of scope*
here and becomes a separate "Agents" menu follow-up.

Key constraints leveraged:
- A global config dir already exists (`GLOBAL_CONFIG_DIR` via platformdirs; holds
  `pricing.yaml`, `availability.yaml`, RL weights). `preferences.yaml` is a sibling.
- Disabling a play is a mask stage in `rl/mask.py`; it does **not** bump the
  action-space version (masking is orthogonal to `ACTION_SPACE_VERSION`).
- Config is frozen `RuntimeConfig` dataclasses with an existing atomic SIGHUP reload swap.
- Desktop never parses YAML; all config flows through Python sidecar JSON-RPC.

## Decisions

**Storage & scope:** Global-only `GLOBAL_CONFIG_DIR/preferences.yaml`, sibling to
`pricing.yaml` — applies to every project on the machine, matching the "global options"
intent and reusing existing global-file + SIGHUP-reload patterns. No project-level
`preferences.yaml`.

**Play-disable granularity:** Curated allowlist only — a single `USER_DISABLEABLE_PLAYS`
constant (e.g. `RUN_QA`, `CLEANUP`, `PRUNE`, `DESIGN_AUDIT`, `GROOM_BACKLOG`). Delivery,
lifecycle, and self-heal plays (`ISSUE_PICKUP`, `CODE_REVIEW`, `MERGE_PR`,
`RECONCILE_STATE`, `END_SESSION`, …) are never user-disableable, so a preference can never
wedge issue delivery.

**Menu surface & sequencing:** Desktop app settings pane is the primary surface and is
built first; the `agentshore preferences` CLI follows. Both read/write the same
`preferences.yaml`.

**Desktop ↔ file wiring:** New sidecar JSON-RPC methods (`preferences.get` /
`preferences.set`), consistent with every other config screen and the no-YAML-in-TypeScript
rule (#123). Python is the sole owner/parser of `preferences.yaml`; Desktop pane, CLI, and
mask all share it. (Not the Tauri plugin-store — that's for cosmetic UI state only; these
preferences drive orchestrator behavior, so Python must be source of truth.)

**Composition & live-reload:** `load_config()` (or a thin wrapper) also reads
`preferences.yaml` and folds the fields into the frozen `RuntimeConfig`. `preferences.set`
writes the file and triggers the existing atomic config-swap (SIGHUP path), so a running
session reflects changes within a tick. One config object, one reload mechanism — no new
architecture. A preference is assumed to be changeable mid-run.

**Disabled-play mask classification:** Hard/structural mask, honored everywhere — treated
like a reserved slot, zeroed unconditionally, and **never** resurrected by the reverse
failsafe or any re-expose path. A dedicated `USER_DISABLED` `MaskReason` keeps observability
honest (dashboard/loop-detector see intent, not a stuck state) and prevents the all-masked
watch from flagging it as a wedge. Safe because the allowlist excludes all delivery-critical
plays.

**Migration precedence:** Global-only replacement (Option A) for every migrated field — the
field leaves `agentshore.yaml` entirely and lives solely in `preferences.yaml`. No layering /
global-default-with-project-override model is built. This keeps the model flat (no merge in
the hot path) and fits the no-legacy/no-back-compat stance. Layering would only be built
later, deliberately, if a per-project override ever proves necessary.

**v1 migration candidate set:** Migrate the genuinely machine-global runtime knobs:
- Timeouts: `agent_timeout`, `play_timeouts`, `agents.<type>.stream_idle_timeout`,
  `agents.<type>.first_byte_timeout_seconds`, `feedback.unanswered_timeout_seconds`,
  `feedback.loop_liveness_timeout_seconds`.
- Drain: `feedback.graceful_drain_timeout_seconds`.
- UX: `ui.theme`, `ui.refresh_rate`, `logging.level`.

Deep RL/reward/PPO tuning fields, though machine-global, are **not** surfaced as
preferences. The file is given a forward-compatible top-level shape (e.g. `plays:`, `ui:`,
`timeouts:`/`runtime:`) so it can grow without churn.

**Agent roster out of scope:** Agent enabled/disabled, models, and max-concurrency counts
are a per-project session choice, not a machine preference. They are removed from the
Preferences feature and become a separate "Agents" menu (see Open Items). Their mid-run
mechanics differ fundamentally — roster changes require agent-manager *reconciliation*
(spawn on enable, drain+retire on disable/lower `max`, free concurrency on raise), which is
lifecycle work touching `INSTANTIATE_AGENT`/`END_AGENT`, claim release, and in-flight drain —
not a config swap.

## Open Items

- **CLI command shape:** `agentshore preferences` subcommand surface (e.g.
  `get`/`set`/`list`/`reset`) not finalized — minor; a `get`/`set`/`list` trio with a
  `--reset` is the assumed default unless changed.
- **Exact `USER_DISABLEABLE_PLAYS` membership:** Starting set is QA/cleanup/prune/
  design-audit/groom-backlog; confirm whether `CALIBRATE_ALIGNMENT` or others belong.
- **Agents menu (mid-run roster editing) — immediate next plan:** A separate Desktop pane to
  edit enabled/models/max mid-run, requiring live agent-manager reconciliation. Reuses the
  `preferences.get/set` RPC pattern and Desktop-pane scaffolding from this plan, but its
  reconciliation/drain design (and its interaction with the known "worktree reclaimed
  mid-play" hazard) needs its own design pass. Documented here so the intent isn't lost.
- **Migration mechanics for moved fields:** Since migration is global-only (no back-compat),
  decide how existing `agentshore.yaml` files that still carry these fields are handled on
  load (ignore-with-warning vs. one-time move) — to be settled at implementation time,
  consistent with the no-legacy-cruft stance.
