import { useEffect, useState, type JSX } from "react";

import type { ResolvedTheme, ThemeMode } from "../theme";

const STORAGE_KEY = "agentshore.dashboard.theme";

function isThemeMode(value: string | null | undefined): value is ThemeMode {
  return value === "system" || value === "light" || value === "dark";
}

/**
 * Map legacy "grid-light"/"grid-dark" stored values (pre-desktop-axub) onto
 * the renamed "light"/"dark" modes so users who set a preference before the
 * rename don't see their selection silently reset to the default.
 */
function migrateLegacyMode(value: string | null | undefined): ThemeMode | null {
  if (value === "grid-light") return "light";
  if (value === "grid-dark") return "dark";
  return null;
}

function readStoredMode(): ThemeMode | null {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (isThemeMode(stored)) return stored;
    const migrated = migrateLegacyMode(stored);
    if (migrated !== null) {
      // Persist the migrated value so the next read short-circuits above.
      window.localStorage.setItem(STORAGE_KEY, migrated);
    }
    return migrated;
  } catch (err) {
    console.warn("[theme] could not read stored theme mode:", err);
    return null;
  }
}

function writeStoredMode(mode: ThemeMode): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch (err) {
    console.warn("[theme] could not persist theme mode:", err);
  }
}

function resolveTheme(mode: ThemeMode): ResolvedTheme {
  if (mode === "system") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }
  return mode;
}

function applyTheme(mode: ThemeMode): ResolvedTheme {
  const resolved = resolveTheme(mode);
  const root = document.documentElement;
  root.dataset.themeMode = mode;
  root.dataset.theme = resolved;
  return resolved;
}

export interface ThemeToggleProps {
  /** Fires when the *resolved* theme changes (after system fallback). */
  onResolvedThemeChange?: (theme: ResolvedTheme) => void;
  /** Optional URL-driven mode. When set, it overrides stored preference. */
  modeOverride?: ThemeMode;
}

/**
 * Three-button theme picker that lives in the dashboard HUD's top-right.
 * Persists the mode to localStorage and tracks the system colour scheme
 * when set to "system" (matches the imperative initThemeController).
 */
export function ThemeToggle({
  modeOverride,
  onResolvedThemeChange,
}: ThemeToggleProps = {}): JSX.Element {
  const [mode, setMode] = useState<ThemeMode>(
    () => modeOverride ?? readStoredMode() ?? "light",
  );

  useEffect(() => {
    if (modeOverride) setMode(modeOverride);
  }, [modeOverride]);

  useEffect(() => {
    const resolved = applyTheme(mode);
    onResolvedThemeChange?.(resolved);
    if (!modeOverride) writeStoredMode(mode);
  }, [mode, modeOverride, onResolvedThemeChange]);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => {
      if (mode === "system") {
        const resolved = applyTheme("system");
        onResolvedThemeChange?.(resolved);
      }
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [mode, onResolvedThemeChange]);

  const allTabs: Array<{ value: ThemeMode; label: string }> = [
    { value: "system", label: "Auto" },
    { value: "light", label: "Light" },
    { value: "dark", label: "Dark" },
  ];
  const tabs: Array<{ value: ThemeMode; label: string }> = modeOverride
    ? allTabs.filter(({ value }) => value === modeOverride)
    : allTabs;

  return (
    <div className="theme-toggle" id="theme-toggle" role="group" aria-label="Theme">
      {tabs.map(({ value, label }) => (
        <button
          key={value}
          type="button"
          className="theme-toggle-btn"
          data-theme-mode={value}
          aria-pressed={mode === value}
          onClick={() => setMode(value)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
