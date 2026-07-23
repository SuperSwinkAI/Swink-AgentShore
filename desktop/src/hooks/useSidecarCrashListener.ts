import { useEffect, useState } from "react";
import type { NavigateFunction } from "react-router-dom";
import {
  subscribeSidecarCrashed,
  type SidecarCrashedPayload,
} from "../services/sidecarEvents";

/** Redirects to /recovery and captures the payload when the sidecar crashes. */
export function useSidecarCrashListener(
  navigate: NavigateFunction,
): SidecarCrashedPayload | null {
  const [crashPayload, setCrashPayload] = useState<SidecarCrashedPayload | null>(null);

  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;
    void subscribeSidecarCrashed((payload) => {
      if (cancelled) return;
      setCrashPayload(payload);
      navigate("/recovery", { replace: true });
    })
      .then((fn) => {
        if (cancelled) {
          fn();
        } else {
          unlisten = fn;
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [navigate]);

  return crashPayload;
}
