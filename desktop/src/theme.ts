import { invoke } from "@tauri-apps/api/core";
import { useCallback, useEffect, useState } from "react";

export type ThemeChoice = "system" | "light" | "dark";

export function normalizeTheme(value: string | undefined): ThemeChoice {
  // Legacy "grid-light"/"grid-dark" stored values (pre-desktop-axub) map
  // onto the renamed "light"/"dark" choices so existing preferences carry over.
  if (value === "dark" || value === "grid-dark") return "dark";
  if (value === "light" || value === "grid-light") return "light";
  return "system";
}

export function resolveThemeChoice(value: ThemeChoice): "light" | "dark" {
  if (value !== "system") {
    return value;
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/**
 * Owns the resolved theme, applies it to the document, and tracks the OS
 * theme when the choice is "system". `setTheme` is exposed so the initial
 * `load_ui_state` hydration (still in App.tsx — it also hydrates setup and
 * onboarding state from the same RPC call) can seed the persisted value.
 */
export function useThemeSync(): {
  theme: ThemeChoice;
  setTheme: (next: ThemeChoice) => void;
  onThemeChange: (next: string) => void;
} {
  const [theme, setTheme] = useState<ThemeChoice>("system");

  useEffect(() => {
    const applyResolvedTheme = () => {
      document.documentElement.dataset.themeMode = theme;
      document.documentElement.dataset.theme = resolveThemeChoice(theme);
    };
    applyResolvedTheme();
    if (theme !== "system") {
      return undefined;
    }
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", applyResolvedTheme);
    return () => {
      mq.removeEventListener("change", applyResolvedTheme);
    };
  }, [theme]);

  const onThemeChange = useCallback((next: string) => {
    const normalized = normalizeTheme(next);
    setTheme(normalized);
    void invoke("set_ui_theme", { theme: normalized }).catch(() => undefined);
  }, []);

  return { theme, setTheme, onThemeChange };
}
