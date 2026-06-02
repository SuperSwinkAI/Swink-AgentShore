## Bucket 09: ui/ + dashboard/ (python)

Scope reviewed: all 24 `.py` files under `src/agentshore/ui/` and the 2 `.py` files under `src/agentshore/dashboard/` (`__init__.py`, `bridge.py`). The "14 files" in the brief counts the bundled static JS/CSS/PNG assets under `dashboard/static/`; there is only one substantive python module there (`bridge.py`, 573 lines). No file crosses 1000 lines; the largest are `app.py` (490) and `bridge.py` (573).

The dominant structural problems are (1) a whole parallel **dead widget + dead screen + dead action surface** that is unit-tested but never wired into the running app, and (2) **business/data-derivation logic baked into render methods and widgets** that belongs in the StateProvider/state layer — including a second, divergent copy of the issue-grouping algorithm. There is also pervasive small-scale duplication of play-label, elapsed-time, truncate, and ActivePlay-from-PlayParams logic across widgets.

---

## Critical

### C1. `ActivePlayWidget` is a fully-built, fully-tested, never-mounted widget (138 lines dead)
**Location:** `src/agentshore/ui/widgets/active_play.py` (entire file, 1-138)
**Problem:** `MainDashboard.compose()` (`screens/dashboard.py:46-56`) mounts `AgentPanel`, `AlertBar`, `AlignmentBars`, `BudgetWidget`, `WorkQueueSummary`, `RLStateBar`, `PlayHistoryTable` — *not* `ActivePlayWidget`. Grep confirms the only references outside the file are in `tests/ui/test_widgets.py`. The active-play display the user actually sees is rendered inside `AgentPanel` (`_agent_play`, `agent_panel.py:96-117`). So `ActivePlayWidget` is a parallel, independently-maintained implementation of the same feature with its own copies of `_as_int`, `_started_monotonic`, `_display_play`, `_format_elapsed`, and `set_play_started` (the last is byte-for-byte duplicated with `AgentPanel.set_play_started`, `agent_panel.py:70-82` vs `active_play.py:76-87`). It is "tested" only against itself, giving false coverage confidence for a screen path that can never execute.
**Code-judo remedy:** Delete `active_play.py` entirely and its test class in `tests/ui/test_widgets.py` (the `ActivePlayWidget` block, ~lines 217-310). Removes ~138 src lines + ~90 test lines, eliminates 5 duplicated helpers, and kills the second `set_play_started` copy. If a standalone active-play widget is genuinely wanted later, resurrect from git — do not keep a dead twin.

### C2. Issue/PR lifecycle grouping is duplicated and divergent across the two UIs
**Location:** `src/agentshore/ui/screens/issues.py:36-101` (`_render_work_queue`) vs `src/agentshore/ui/widgets/work_queue.py:22-69` (`WorkQueueSummary.render` + `_is_in_progress`/`_next_issue`)
**Problem:** Two independent reimplementations of "what phase is each issue/PR in." `issues.py` derives TO DO / IN PROGRESS / IN REVIEW / DONE by cross-referencing `state.agents`, `state.pull_requests`, `state.pending_review_queue`, and `issue_numbers_for_pr`. `work_queue.py` recomputes a *different* notion of the same thing (ready/in-progress via `bead_status` string-normalization in `_is_in_progress`, plus its own `_next_issue` priority sort). The two will drift: e.g. `work_queue._is_in_progress` keys off `bead_status` strings while `issues.py` keys off `agent.current_play_*`. This is exactly the "business logic in the widget" anti-pattern — lifecycle classification is a property of orchestrator state, not of a renderer.
**Code-judo remedy:** Add a single derivation on the state layer — e.g. `OrchestratorState.work_queue() -> WorkQueueView` (typed dataclass: `todo/in_progress/in_review/done` lists + `next_issue`) in `agentshore/state.py`, computed once per snapshot. Both `issues.py` and `work_queue.py` then become pure formatters over that view. Removes the duplicated grouping (~50 lines net) and the divergence risk; `_is_in_progress`/`_next_issue`/`prs_by_issue`/`reviewing_issues` all move out of the UI.

---

## High

### H1. Dead/unwired action surface on `OrchestratorApp` (toggle_pause, show_learnings) — tested but unreachable
**Location:** `src/agentshore/ui/app.py:355-367` (`action_toggle_pause`), `app.py:431-490` (`action_show_learnings`, 60 lines incl. an inline `LearningsModal` class defined inside the method)
**Problem:** `BINDINGS` (`app.py:83-90`) maps keys only to `drain_session`, `hard_quit`, `show_help`, `show_goals`, `show_agent_detail`, `show_issues`. There is **no binding and no caller** for `action_toggle_pause` or `action_show_learnings` (grep-confirmed). A user can never pause via the TUI, and can never open learnings. `action_show_learnings` is the single most complex method in the file: it loads learnings off-thread, queries play history from the store, builds a fixed-width ASCII table, and defines a `ModalScreen` subclass *inline*. All of this is dead. Worse, `tests/ui/` exercises these methods directly, so the suite asserts behavior the live binding table makes unreachable — false confidence again.
**Code-judo remedy:** Either wire them (add `("p","toggle_pause",...)` / `("l","show_learnings",...)` to `BINDINGS`) or delete them. If deleting: remove `action_toggle_pause` (13 lines), `action_show_learnings` + inline `LearningsModal` (60 lines), and the now-unused `import json` / `import sqlite3` and the `_short_play_label`/`_PLAY_SHORT_LABEL` table (`app.py:30-57`) which exists *solely* to format the learnings source column. That's ~100 lines plus a 23-entry dict gone from the app shell. Decide with the product owner, but do not leave a tested-yet-unbound action.

### H2. `set_summary` / `set_play` are dead public API with live tests
**Location:** `src/agentshore/ui/screens/shutdown.py:92-93` (`set_summary`) + the `_summary` reactive (`shutdown.py:52`) + `watch__summary` (144-152) + `_render_summary` (178-185); `src/agentshore/ui/widgets/active_play.py:69-74` (`set_play`)
**Problem:** Grep across `src/` shows **zero callers** of `SessionEndScreen.set_summary` and zero callers of `ActivePlayWidget.set_play`. The entire `_summary` reactive + watcher + renderer pipeline in `shutdown.py` is plumbing for a feature nothing drives — the session-end screen never shows a summary. `set_play` is dead alongside its already-dead host widget (C1).
**Code-judo remedy:** Delete `set_summary`, the `_summary` reactive, `watch__summary`, `_render_summary`, and the `yield Static("", id="summary")` in `compose` (`shutdown.py:60`). ~25 lines. `set_play` dies with C1.

### H3. Non-functional loop-alert affordances — the alert advertises keys that do nothing
**Location:** `src/agentshore/ui/widgets/alert_bar.py:16-20` (renders `"[R]evert [O]verride [Q]uit"`); `src/agentshore/ui/screens/dashboard.py:73-83` (`show_loop` trigger)
**Problem:** When `loop_level == 3`, the dashboard calls `AlertBar.show_loop`, which renders a banner promising "[R]evert [O]verride [Q]uit". But there are **no `r`/`o` key handlers** on `MainDashboard`, `AlertBar`, or `OrchestratorApp` (grep for `action_revert`/`action_override`/`revert`/`override` in the UI returns nothing but the unrelated `revert.py` modal, itself never pushed — see H4). Only `q` (quit) works, and only because it's a global app binding. The banner lies to the operator during exactly the loop-wedge scenario your MEMORY.md flags as critical (issue #9 auto-stop). This is a correctness/UX defect, not just dead code.
**Code-judo remedy:** Either implement `r`/`o` handlers on `MainDashboard` that call the orchestrator (revert last commit / override the masked play) — wiring in `RevertConfirmModal` (H4) for the `r` path — or change the rendered string to only advertise what works (`"— [Q]uit or wait for auto-stop"`). Given the loop-wedge auto-stop invariant, prefer implementing or removing the affordance text; do not ship a banner that names dead keys.

### H4. `RevertConfirmModal` screen built and exported but never pushed
**Location:** `src/agentshore/ui/screens/revert.py` (entire file, 36 lines); exported in `src/agentshore/ui/screens/__init__.py:10,20`
**Problem:** A complete confirm-modal returning `bool`, exported from the package `__init__`, with tests — but `app.push_screen(RevertConfirmModal())` appears nowhere in `src/` (only in tests). It is the missing other half of H3's `[R]evert` affordance. Right now it is pure dead weight that looks load-bearing because it's exported and tested.
**Code-judo remedy:** Tie it to the H3 `r` handler (then it's live), or delete the file + the two `__init__.py` lines + the test. Don't leave it exported-but-unreachable.

## Medium

### M1. Three divergent PlayType→label formatters
**Location:** `src/agentshore/ui/app.py:30-57` (`_PLAY_SHORT_LABEL` dict + `_short_play_label`), `src/agentshore/ui/widgets/rl_state.py:76-77` (`display_play`), `src/agentshore/ui/widgets/active_play.py:128-129` (`_display_play`)
**Problem:** Three functions convert a `PlayType` to a human label, two of them identical (`display_play` and `_display_play` are both `play_type.value.replace("_", " ").title()`), the third a hand-maintained 23-entry abbreviation dict. `dashboard.py` imports `display_play` from `rl_state` — a widget — purely for the label helper, coupling the screen to a widget for a string transform. Labels for the *same* play type therefore differ across surfaces with no single source of truth.
**Code-judo remedy:** Put `play_label(pt)` and `play_short_label(pt)` on `PlayType` itself (or a tiny `ui/play_labels.py` already adjacent to `alignment_levels.py`). Delete `_display_play`, `display_play`, `_short_play_label`, and `_PLAY_SHORT_LABEL` from their current homes; import the canonical pair everywhere. ~30 lines and one widget→screen coupling removed. (If C1/H1 land, two of these three vanish anyway.)

### M2. Duplicated `_truncate`, `_as_int`, and elapsed/started time helpers across widgets
**Location:** `_truncate` in `agent_panel.py:166-169` and `work_queue.py:72-75` (identical); `_as_int` in `agent_panel.py:152-155` and `active_play.py:110-113` (identical); elapsed/timestamp math split across `agent_panel._elapsed_label` (135-149), `active_play._started_monotonic` (116-125), `active_play._format_elapsed` (132-137) — all parsing the same ISO-with-Z timestamp.
**Problem:** Four small numeric/string helpers copy-pasted between widgets, plus three near-identical ISO-timestamp parsers. The `.replace("Z","+00:00")` + tz-fill + `ValueError`-swallow dance appears three times.
**Code-judo remedy:** A `ui/format.py` with `truncate(s, n)`, `as_int(v)`, `parse_started(iso) -> datetime | None`, and `human_elapsed(seconds)`. Every widget imports from there. ~40 duplicated lines collapse to one module; the ISO-parse bug surface shrinks to one place.

### M3. `MainDashboard` derives loop/alert state imperatively from raw streak counters in the render path
**Location:** `src/agentshore/ui/screens/dashboard.py:60-83` (`on_orchestrator_app_state_updated`), depending on `loop_level_for_streak` imported from `rl_state.py:65-73`
**Problem:** The screen reaches into `event.state.same_type_failure_streak`, recomputes `loop_level_for_streak` (a magic-number ladder: 3/5/7), and manually toggles `self._loop_alert_active` to drive `alert.show_loop()`/`alert.hide()`. The same `loop_level_for_streak` ladder is *also* recomputed inside `RLStateBar.render`/`update_state` (`rl_state.py:36-43,57-61`). Loop-escalation level is a derived property of orchestrator state being recomputed in two render paths with a hand-rolled `_loop_alert_active` diff flag — the "manual state-diffing that should be declarative reactive state" anti-pattern.
**Code-judo remedy:** Expose `loop_level: int` (and optionally `loop_play_label`) on `OrchestratorState` / its snapshot, computed once in core. Widgets switch on the typed field; `RLStateBar` and `MainDashboard` stop importing `loop_level_for_streak`. The `_loop_alert_active` bookkeeping becomes a straight `alert.set_loop_level(state.loop_level)` call that the AlertBar reacts to internally. Removes the duplicated ladder and the manual diff flag (~15 lines + one cross-widget import).

### M4. `app.py` message handlers reach back into `_latest_state` and mutate it in place
**Location:** `src/agentshore/ui/app.py:267-279` (`on_..._agent_changed` rewrites `_latest_state.agents`), `app.py:287-293` (`session_paused` sets `_latest_state.session_state`), `app.py:295-310` (`session_draining` mutates `session_state` + `drain_reason`)
**Problem:** The app shell patches a mutable `OrchestratorState` it received from the provider — `replace(agent, status=...)` list rebuilds, direct `.session_state = SessionState.PAUSED` writes, etc. — so the UI carries a hand-maintained shadow copy of orchestrator state that can diverge from the next authoritative snapshot, and screens then read `getattr(self.app, "_latest_state", None)` (`dashboard.py:102-133`) to pull from it. This is business-state reconstruction in the view layer.
**Code-judo remedy:** Treat each `StateUpdated` snapshot as immutable and authoritative; drop the eager in-place mutations in the three handlers and let the next snapshot carry status/session-state changes (the provider already emits a full `on_state_update`). If the eager hint is needed for latency, route it through a typed `apply_hint()` on the state object rather than ad-hoc field writes scattered across handlers. Removes ~20 lines of shadow-state patching and the `getattr(..., "_latest_state", None)` stringly back-reach in `dashboard.py`.

## Low

### L1. `TuiStateProvider` repeats `from agentshore.ui.app import OrchestratorApp` in all 11 methods
**Location:** `src/agentshore/ui/provider.py:29-89` (the import appears 12×, once per method body)
**Problem:** Every method re-imports `OrchestratorApp` locally to dodge a circular import. It's noise and obscures the one real concern (the cycle).
**Code-judo remedy:** Import once at module load behind the existing lazy boundary, or hold the message classes on `self._app.__class__`. Even a single module-level `import` guarded by the existing `TYPE_CHECKING` split + one runtime import at top of file removes 11 repetitions. ~11 lines.

### L2. `EscalationModal` uses a `Static` as an invisible mount anchor and imports `QueryError` five times locally
**Location:** `src/agentshore/ui/screens/escalation.py:29` (`Static("", id="budget-input-row")` exists only as an `after=` anchor), and the repeated `from textual.css.query import QueryError` inside `_show_budget_input`/`_hide_budget_input`/`_submit_budget` (47,68,75)
**Problem:** Budget input is built by `self.mount(... after="#budget-input-row")` against an empty placeholder Static, with show/hide done by mounting/removing three widgets each time — imperative DOM surgery where a single reactive `show_budget_input: bool` + a pre-composed (hidden) container would be declarative. `QueryError` is re-imported in three methods (and again in `shutdown.py`, `startup.py`, `escalation.py` — a repo-wide local-import habit in this bucket).
**Code-judo remedy:** Pre-compose the budget Input + confirm/cancel buttons inside a `#budget-input-row` container that starts with `display:none`, toggle a `reactive[bool]`; delete `_show_budget_input`/`_hide_budget_input` mount/remove logic (~25 lines). Hoist `QueryError` to a module import in each file.

### L3. `bridge.py` `_ingest_event_line` ignores its own `broadcast` parameter via `_ = broadcast`
**Location:** `src/agentshore/dashboard/bridge.py:381-433` (esp. the `_ = broadcast` no-op at 433, with `broadcast` only consulted for the two early-return lifecycle branches at 393-404)
**Problem:** The `broadcast` flag is half-honored: it gates whether `session_draining`/`session_ended` raw lines are cached, but the play-event branch updates `_active_play`/`_event_history` unconditionally and then explicitly discards the flag with `_ = broadcast` plus a comment apologizing for it. The parameter's contract is muddy — it means "is this live vs prime-from-disk" but only changes behavior for two of three message types.
**Code-judo remedy:** Either make the play-event path respect `broadcast` consistently (don't mutate live caches during silent priming if that's the intent), or split into two methods — `_ingest_live(line)` and `_ingest_prime(line)` — so the flag and the `_ = broadcast` apology disappear. Minor, but it's a confusing flag in the highest-traffic method of the bridge.

### L4. `bridge.py` `__init__` accepts both `socket_path` and `ipc_endpoint` with a normalization dance
**Location:** `src/agentshore/dashboard/bridge.py:59-81`
**Problem:** Two ways to pass the same thing (`socket_path: str|Path|IpcEndpoint|None` *and* `ipc_endpoint: IpcEndpoint|None`), reconciled by an 11-line `if/elif/isinstance` block, plus a redundant `self._socket_path = self._ipc_endpoint.path` kept around for the unix-only branch in `_connect_ipc` (548-552). The `socket_path`-as-`IpcEndpoint` overload is a thin back-compat wrapper.
**Code-judo remedy:** Take `ipc_endpoint: IpcEndpoint` as the single required arg; let callers do `IpcEndpoint.unix(path)` at the call site. Drop `socket_path` and `_socket_path`; in `_connect_ipc` use `self._ipc_endpoint.path` directly. ~12 lines and one redundant field removed. (Verify no caller passes the legacy positional first — quick grep before deleting.)
