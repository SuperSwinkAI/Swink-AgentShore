# Welcome Carousel — First-Time User Flow (Desktop)

Design decisions for a dismissable, replayable first-time welcome flow in the
AgentShore desktop app (Tauri v2 + React 19). This doc captures *decisions*, not
implementation — it is the spec to build against.

## Purpose & shape

- **Form**: a centered, click-through **welcome carousel** (modal over a dimmed
  app). Purpose = orient a first-timer on *what AgentShore is* and *what they
  need to get started*. It is **not** an interactive coachmark/spotlight tour —
  the existing 6-step setup wizard (`SetupLayout`) already hand-holds
  configuration, so an anchored tour would duplicate it and be fragile against
  layout changes.

## Trigger

- Fires on **first launch**, layered as a modal **over `ChooseProjectScreen`**
  (the real app is visible, dimmed, behind it).
- Gated **solely on the persisted "seen" flag**. The desktop app always boots to
  `ChooseProjectScreen` (recovery/fatal-error/starting are navigated *to*, not
  boot routes), so no route-gating is required.

## Dismissal state machine

- Reaching the **last slide** → marks seen (flag `true`).
- Ticking **"Don't show again"** → marks seen.
- **Early close** (X button or Esc before the last slide, checkbox unchecked) →
  flag stays `false`; carousel re-shows on next launch.
- **Backdrop click does NOT close** (prevents accidental dismissal — overrides
  any default backdrop-close behavior in the shared modal).
- **Upgrade behavior**: the flag is a new field. Absent value = not seen, so
  existing users see the carousel once on the release that ships this. No
  "are they really new?" heuristic — one boolean, simple and harmless.

## Persistence

- New boolean **`onboarding_completed`** stored in the **Tauri store**
  (`ui-state.json`, the `UiState` struct in `desktop/src-tauri/src/lib.rs`),
  mirroring the existing `set_ui_theme` pattern (struct field + Tauri command).
- Chosen over localStorage because this is a durable app-level user preference
  (like theme/window geometry), and the native store survives a webview/
  localStorage reset.

## Content — 4 slides

Text + theme styling only (no sprites; an icon/emoji per slide at most).

1. **Welcome / What is AgentShore** — an RL orchestrator that coordinates CLI
   coding agents (Claude Code, Codex, Grok, Antigravity) to work your backlog.
   *It decides what to do next and who does it — it does not write the code.*
2. **How a session works** — pick a repo & budget → agents pick up issues, open
   PRs, and review each other → you watch it happen on the dashboard.
3. **What you'll need** — a git repo; **at least 2 supported agent CLIs
   installed**; **at least 2 GitHub accounts**. AgentShore has agents review each
   other's PRs and a reviewer can never approve their own work, so review only
   happens with a second agent + identity. **Recommended:** give each agent
   harness its own GitHub identity (one per harness) for fully auditable
   attribution.
4. **Get started** — primary button **"Get started"** closes the carousel and
   hands off to `ChooseProjectScreen` underneath (no auto file-dialog — the user
   picks from recents or opens a folder themselves).

## Interaction

- **Next / Back buttons + progress dots** (dots show position and allow jumping).
- **X button top-right, always present** — the early-close path.
- **Keyboard**: Esc closes (= early close unless on/past the last slide);
  ←/→ navigate.
- **Last slide**: primary CTA reads **"Get started"**; reaching this slide marks
  the carousel seen.

## Replay (Help menu)

- New native Help-menu item **"Welcome Tour"** at the **top of the Help menu**
  (`desktop/src-tauri/src/lib.rs`), above "Documentation".
- Emits a Tauri menu event (same pattern as `menu:keyboard_shortcuts`);
  `App.tsx` / `AppMenu` listens and opens the carousel.
- **Replay never auto-mutates the flag.** The "Don't show again" checkbox is
  always visible and bound to the stored preference: pre-checked if already
  suppressed; unchecking it resumes auto-show on future launches.

## Testing

- **Component / RTL tests**: slide rendering; Next/Back/dots navigation; X/Esc =
  early close (flag untouched); last-slide reached + checkbox = flag set; replay
  opens the carousel.
- **Rust unit test**: `UiState.onboarding_completed` field + command round-trip
  (persist → read).
- **No E2E** — the dismissal state machine is fully unit-testable and the
  persistence path is covered by the Rust test.

## Files in scope (for implementation)

- `desktop/src-tauri/src/lib.rs` — add `onboarding_completed` to `UiState`, the
  get/set command, and the "Welcome Tour" Help-menu item + event.
- `desktop/src/App.tsx` — read the flag on mount, conditionally render the
  carousel over `ChooseProjectScreen`, and listen for the replay menu event.
- `desktop/src/components/WelcomeCarousel.tsx` (+ `.module.css`) — new component;
  reuse the theme-aware overlay/dialog styling patterns from
  `desktop/src/components/AppMenu.module.css`.
