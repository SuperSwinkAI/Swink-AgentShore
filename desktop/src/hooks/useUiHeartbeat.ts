import { invoke } from "@tauri-apps/api/core";
import { useEffect } from "react";

/**
 * Phase 2 — heartbeat (#274). Fires invoke("ui_heartbeat") on mount and
 * every ~2s to let the Rust watchdog detect a JS-alive paint wedge.
 * Each beat is scheduled from within a requestAnimationFrame callback so
 * the beat STOPS if rAF stalls (the only JS-observable signal of a paint
 * wedge). A bare setInterval would keep firing even through a wedge.
 */
export function useUiHeartbeat(): void {
  useEffect(() => {
    let mounted = true;
    let rafId: ReturnType<typeof requestAnimationFrame> | undefined;
    let timerId: ReturnType<typeof setTimeout> | undefined;

    const scheduleBeat = () => {
      rafId = requestAnimationFrame(() => {
        if (!mounted) return;
        void invoke("ui_heartbeat").catch(() => undefined);
        timerId = setTimeout(scheduleBeat, 2000);
      });
    };

    scheduleBeat();

    return () => {
      mounted = false;
      if (rafId !== undefined) cancelAnimationFrame(rafId);
      if (timerId !== undefined) clearTimeout(timerId);
    };
  }, []);
}
