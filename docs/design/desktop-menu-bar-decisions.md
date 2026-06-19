# Desktop App Menu Bar — Decisions

## Overview
The Tauri desktop app (`desktop/src-tauri/src/lib.rs:585-629`, Tauri 2.11) already ships a
working native menu bar: **File** (Adjust Budget, Stop Session, Close Window), **Edit**
(standard items), **View** (Fullscreen only), **Window** (Minimize, Maximize), and the
macOS-supplied App menu (About/Hide/Quit). The `tauri-plugin-updater` is wired
(`tauri.conf.json:64-69`, pointed at the GitHub `latest.json`) but has no menu entry; there
is no system tray and no Settings/Preferences UI.

The goal of this pass is **full standard-app parity** for the native menu bar — filling the
gaps that make the app read as "unfinished" — without taking on net-new product surface
beyond what's required. Config remains canonical in `agentshore.yaml` (immutable frozen
dataclasses, SIGHUP reload); UI-shell state continues to live in `ui-state.json` via
`tauri-plugin-store`.

## Decisions

**Overall scope:** Full standard parity (Option B) — Help menu, Check for Updates, and a
Settings/Preferences window; tray excluded — because a long-running orchestrator people leave
open should hit native first-class-app polish, and the missing Help menu / absent Preferences
are the two things that read as unfinished.

**System tray:** Deferred to a separate follow-up — it's net-new product surface (icon status
states, close-to-tray, notifications) that interacts with the existing quit/teardown
invariants (`ExitRequested`, `QuitConfirmed` latch, teardown watchdog at lib.rs:758-775) and a
different Tauri subsystem (`TrayIconBuilder`), so it earns its own design conversation rather
than riding along on a menu-completeness pass.

**Help menu:** New menu with **links** (Documentation, Report an Issue, Release Notes →
external URLs), a **Keyboard Shortcuts** cheat-sheet (in-app surface for existing accelerators
like Cmd+B, Cmd+Shift+.), and **diagnostics** (Open Log Folder + Copy Diagnostics:
version/OS/session id) (Option C) — because users will file GitHub issues against this app, and
logs + copyable diagnostics turn vague reports into actionable ones at near-zero build cost.

**View menu:** Left as-is — Fullscreen only. Reload/Zoom/DevTools intentionally skipped.

**Window menu:** Left as-is — Minimize/Maximize only. Zoom / Bring All to Front skipped (low
payoff for a single-window app).

**Check for Updates:** Manual "Check for Updates…" menu item **plus** a silent check on launch
that prompts only when an update exists (Option B) — the standard consumer-app behavior;
periodic in-session re-checking was rejected as intrusive during live orchestration runs.

## Open Items

- **Settings/Preferences window:** Deferred — another agent is actively researching the
  options. The menu work should route the Cmd-, accelerator and placement around whatever that
  research lands on. Note the architectural fork already identified: UI-shell prefs (theme,
  notifications, window behavior) belong in `tauri-plugin-store`/`ui-state.json` and have no
  business in `agentshore.yaml`; any config *editing* would have to respect the
  immutable-dataclass / SIGHUP-reload model and is a much heavier, riskier surface.
- **Check for Updates placement convention:** Implementation detail — conventionally the App
  menu on macOS, the Help menu on Windows.
