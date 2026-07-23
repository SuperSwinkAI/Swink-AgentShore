import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useCallback, useEffect, useState } from "react";

/**
 * First-run welcome carousel. `onboardingSeen` mirrors the persisted
 * `onboarding_completed` flag (Tauri store); `welcomeOpen` is the live
 * visibility. They diverge on replay (Help ▸ Welcome Tour opens it without
 * touching the flag) and on early-close (hidden but not yet seen).
 *
 * `setWelcomeOpen` and `setOnboardingSeen` are exposed so App's initial
 * `load_ui_state` hydration (a single RPC that also seeds theme + setup
 * state, so it stays in App.tsx) can seed both values on mount.
 */
export function useWelcomeOnboarding(): {
  welcomeOpen: boolean;
  setWelcomeOpen: (next: boolean) => void;
  onboardingSeen: boolean;
  setOnboardingSeen: (next: boolean) => void;
  markWelcomeSeen: () => void;
  setWelcomeSeen: (next: boolean) => void;
} {
  const [welcomeOpen, setWelcomeOpen] = useState(false);
  const [onboardingSeen, setOnboardingSeen] = useState(true);

  // Replay: Help ▸ Welcome Tour re-opens the carousel without mutating the
  // persisted flag (only the carousel's own checkbox / reaching-the-end does).
  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;
    void listen("menu:welcome_tour", () => {
      if (!cancelled) {
        setWelcomeOpen(true);
      }
    })
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  // Persist the welcome flag. `markWelcomeSeen` is fired when the user reaches
  // the last slide; it only writes on the false→true transition so reaching the
  // end repeatedly doesn't spam IPC. `setWelcomeSeen` backs the checkbox, which
  // can flip the flag either way (unchecking on replay resumes auto-show).
  const markWelcomeSeen = useCallback(() => {
    setOnboardingSeen((prev) => {
      if (!prev) {
        void invoke("set_onboarding_completed", { completed: true }).catch(
          () => undefined,
        );
      }
      return true;
    });
  }, []);
  const setWelcomeSeen = useCallback((next: boolean) => {
    setOnboardingSeen(next);
    void invoke("set_onboarding_completed", { completed: next }).catch(
      () => undefined,
    );
  }, []);

  return {
    welcomeOpen,
    setWelcomeOpen,
    onboardingSeen,
    setOnboardingSeen,
    markWelcomeSeen,
    setWelcomeSeen,
  };
}
