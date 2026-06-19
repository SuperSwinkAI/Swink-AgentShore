# Core — Functional Design

## Responsibility

The core is AgentShore's composition root. It owns the session lifecycle, per-tick state refresh, RL play selection, play execution, reward and experience persistence, online policy updates, feedback/drain/shutdown handling, and state publication to TUI or IPC consumers. The core decides *what to do next and which agent does it*; it never generates code itself.

## Design: Composition Root, Not Inheritance

`Orchestrator` (`orchestrator.py`) is a thin composition root. It inherits a single base, `_OrchestratorBase` (`base.py`), which supplies `__init__` and constructs every owned component, then delegates each public method to the component that owns the behaviour. The earlier 7-mixin MRO has been fully dissolved into composition: the orchestrator *owns* its collaborators as fields and forwards to them.

Why composition over the old mixins: each behaviour area lives in its own file under a LOC budget, components are unit-testable in isolation, and cross-component calls flow through narrow host Protocols (`_LoopHost`, `_DispatcherHost`, `_CompletionHost`, `_DrainHost`, `_StateBuilderHost`, `_LifecycleHost`). Every component reads orchestrator runtime/control state *live* through `self._host.<attr>` rather than capturing it at construction, so a SIGHUP config swap or per-tick mutation is never stale. Stable services (store, manager, executor, session id) are captured once via constructors.

### Owned Components (the former mixins)

| Component | File | Responsibility |
|-----------|------|----------------|
| `LoopRunner` | `mixins/loop.py` | The conductor: main tick loop, loop-detection ladder, stagnation escalation, idle backoff, loop-liveness watchdog. Constructed last; references every sibling. |
| `Dispatcher` | `mixins/dispatch.py` | Override consumption, selector calls, dispatch, action-mask handling. |
| `CompletionProcessor` | `mixins/completion.py` | Play-completion harvesting, RL experience persistence, learnings update, GitHub refresh, agent health callbacks. |
| `StateBuilder` | `mixins/state.py` | DB reads + live handles → `OrchestratorState`; action-mask annotation. |
| `SnapshotProjector` | `mixins/snapshots.py` | Projects DB records/history into snapshot dataclasses; trajectory and play-streak math. |
| `LifecycleController` | `mixins/lifecycle.py` | Pause/resume, SIGHUP config reload, feedback cadence, budget-drain initiation. |
| `DrainController` | `mixins/drain.py` | Graceful drain, stop/hard_stop, budget adjustment, end-session report. |

`_OrchestratorBase` also hosts stateless shared infrastructure (`_safe_call`, which logs-and-swallows exceptions for fire-and-forget coroutines) and a few thin delegators to `LoopRunner` (`initiate_autonomous_stop`, `check_stagnation_escalation`, the loop-liveness watchdog start/stop) so host-Protocol references resolve on the composition root.

## Orchestrator Loop

The single asyncio loop (`run_until_idle` → `LoopRunner`) runs: observe state → PPO selects a play → execute via agent → reward → update policy → repeat.

1. Refresh GitHub (on the issue-refresh interval), beads, agents, budget, stats, and masks into `OrchestratorState`.
2. Publish state to the configured provider.
3. If draining, keep ending idle agents until shutdown can complete.
4. Ask the RL selector for a play and parameters — unless a user/executor override is queued (single-consume FIFO).
5. Execute the play through `PlayExecutor`.
6. Refresh GitHub/beads-derived state affected by the play.
7. Compute alignment delta, reward, failure/streak updates, and policy experience.
8. Persist play, reward, and RL experience.
9. Run online PPO updates/checkpoints when configured intervals are reached.
10. Check budget, loop-detection, terminal no-work, drain, and shutdown conditions.

A selection-state **digest gate** skips the selector (and a storm-prone log line) when nothing the selector cares about changed since the last attempt. It pairs with a Fibonacci-style **idle backoff** that stretches the loop's wait timeout (1s → 21s) the longer the digest holds, so overrides and human pauses are still picked up within ~21s even when no in-flight play wakes the loop earlier.

Scope validation enforces issue-inflation limits after each skill-backed play; artifact drift is logged as evidence only until AgentShore has reliable beads-native path boundaries. See [../rl](../rl), [../plays](../plays), [../agents](../agents).

## Startup: Bootstrap Phase Pipeline

`Orchestrator.bootstrap()` is a classmethod that constructs and wires every component, returning an instance ready to use as an async context manager (`async with await Orchestrator.bootstrap(...) as orch: await orch.run_until_idle()`).

Bootstrap is split into ordered, independently unit-testable `_phase_*` free functions (`phases.py`). Phase ordering is load-bearing: logging is set up first; the datastore must exist before the manager/executor; metrics must exist before the PPO selector; and the session row must be inserted before any FK-referencing write (skills install, GitHub cache, learnings load). The pipeline, in order: init datastore → reset session-scoped tables → init executor/manager/GitHub → init metrics → cleanup stale weights → init PPO selector → create session row → init worktree manager → session-start worktree sweep → clear stale beads in-progress → dirty-trunk baseline → git-safety sweep (default branch + main-repo HEAD) → install skills → fetch GitHub → ensure labels → load learnings → queue initial agent instantiation.

`agentshore init` (separate from `start`) is the explicit setup command for config, identities, and beads. CLI `agentshore start` refreshes project skill templates automatically before dispatch so CLI and desktop startup use the same current bundled skills.

### Async Context Manager

`__aenter__` recovers from prior crashes (abandon unfinished plays and active work claims), starts the agent `HealthMonitor`, arms the loop-liveness watchdog, acquires an OS power assertion to prevent idle sleep during a session, and starts the optional SQLite `IntegrityMonitor`. `__aexit__` calls `stop()`.

## Play Execution

The `PlayExecutor` dispatches **skill-backed plays** through an agent (context file → skill render → dispatch → parse result → update cache → `PlayOutcome`) and runs **internal plays** (`INSTANTIATE_AGENT`, `END_AGENT`, `END_SESSION`, `TAKE_BREAK`, reserved slots) directly without invoking a coding agent. Internal plays let the orchestrator manage its own fleet and lifecycle inside the same action space the PPO policy selects from.

## Work Claims

The resolver and store use `work_claims` to prevent duplicate issue pickup and duplicate PR review/unblock/merge, and to serialize session-scoped work. Claims are superseded when the underlying issue/PR closes or when work is abandoned at shutdown or startup recovery.

## Feedback and Drain

Default operation is autonomous; the PPO policy drives all direction. Human feedback is requested only for escalation cases — budget exhaustion, loop/stagnation escalation, ambiguous intake, or explicit user commands. An escalation pause that no human answers within `feedback.unanswered_timeout_seconds` **auto-stops** the session rather than wedging the loop indefinitely (#9). Explicit `user_request`/`ipc_request` pauses are exempt — an operator who paused is present.

Graceful stop uses **drain mode**: new work stops, running agents finish, and `END_AGENT` handles cleanup until shutdown can complete. **Hard stop** bypasses drain, cancels in-flight plays, and kills agents immediately. A shutdown-time **end-of-session report** can be requested; in embedded/desktop mode the report is surfaced in-app via a ready callback instead of opening a browser.

An independent **loop-liveness watchdog** force-drains the session if the core-loop heartbeat goes stale — a hard freeze the idle/unanswered-pause backstops cannot catch. It is a no-op when `feedback.loop_liveness_timeout_seconds` is unset.

An **optional graceful-drain watchdog** (#180) escalates a graceful drain to the hard stop once `feedback.graceful_drain_timeout_seconds` elapses with plays still in flight, so a stuck play can no longer hang `stop` for hours. It is **opt-in and defaults to `None` (unbounded)**: the design intent is that a drain always lets agents complete their work, and a wall-clock cap cannot distinguish a wedged drain from a healthy-but-slow one (e.g. a large fleet draining serially via `end_agent`), so a non-`None` default would hard-kill in-flight agent work mid-task. Set it only when a deployment specifically needs the backstop. The escalation calls `stop()` from inside the watchdog task; `stop()` must therefore **not** cancel its own running task (`_stop_graceful_drain_watchdog` is a no-op on the self-call path), and the teardown invariant is that **once `stop()` commits `stopped=True`, `do_stop()` always runs** — it is the only path that cancels in-flight agents, checkpoints the WAL, marks the session `stopped`, and sets `stop_done`.

> **Drain-state recovery signature.** A session stuck in `DRAINING` with **no `shutdown_begin` log line** after a `graceful_drain_deadline_escalation` means teardown was aborted before `do_stop()`. The log fingerprint is `loop_terminating` followed by `cli_dispatch_done` events with **no trailing `play_completed`** and no `shutdown_*` steps — in-flight agents drain naturally but are never harvested or finalized, and any second `stop()` caller blocks forever on `stop_done`. The fixed teardown guarantees `do_stop()` runs even when a completion is being processed at the escalation instant.

## Loop Detection

Two distinct mechanisms guard against the policy collapsing onto a repeated action.

**Failure-streak ladder** — `same_type_failure_streak` maps to an escalation level (`loop_level_for_streak`, surfaced in `OrchestratorState.loop_level`):

| Failure streak | Level | Behaviour |
|----------------|-------|-----------|
| `>= 3` | warn | Loop penalty begins; warning state surfaced. |
| `>= 5` | force | Escalation level surfaced. The repeating play type is *not* hard-masked — the policy is expected to diversify on its own. |
| `>= 7` | escalation | Human escalation / autonomous-stop path. |

**Stagnation ladder** — a separate, configurable escalation (`check_stagnation_escalation`, thresholds from `rl.stagnation`) that raises exploration entropy at the first stage, surfaces to a human at the next, and pauses at the last. The entropy boost (`STAGNATION_ENTROPY_MULTIPLIER`) lets the policy break out without force-masking.

A third guard lives in reward shaping (see [../rl](../rl)): an any-outcome `same_type_streak >= 6` applies a reward penalty so the policy does not collapse onto a cheap, repeatedly-successful loop.

## State Providers

The core publishes through the `StateProvider` protocol: a TUI provider (Textual solo mode), an IPC provider (embedded/headless agent mode, Unix socket or TCP), and a null provider (tests). Dashboard mode is a browser bridge layered on top of IPC. The protocol decouples the core from its consumers so the same loop drives every UI/transport mode.
