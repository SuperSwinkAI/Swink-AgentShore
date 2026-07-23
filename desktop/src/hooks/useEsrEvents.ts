import { useEffect } from "react";
import type { NavigateFunction } from "react-router-dom";
import {
  esrPayloadFromReadyParams,
  type EsrPayload,
} from "../services/sessionContext";
import { subscribeCompleted } from "../services/sessionClient";
import { subscribeSidecarNotification } from "../services/sidecarEvents";

/**
 * Routes the shell to the End-Session-Report screen from either of its two
 * triggers: the full `session.completed` payload, or issue #561's
 * `$/esr_ready` notification (drain.py's embedded-mode replacement for
 * `webbrowser.open`), which arrives earlier with enough payload to render
 * the generated HTML report. Whichever fires first navigates; a later
 * `session.completed` can still overwrite the lightweight placeholder.
 */
export function useEsrEvents(
  navigate: NavigateFunction,
  setEsr: (payload: EsrPayload | null) => void,
): void {
  useEffect(() => {
    const unsubscribe = subscribeCompleted((payload) => {
      setEsr(payload);
      navigate("/session/esr");
    });
    return unsubscribe;
  }, [navigate, setEsr]);

  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;
    void subscribeSidecarNotification((payload) => {
      if (cancelled) return;
      if (payload.method !== "$/esr_ready") return;
      const readyPayload = esrPayloadFromReadyParams(payload.params);
      if (readyPayload !== null) {
        setEsr(readyPayload);
      }
      navigate("/session/esr");
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
  }, [navigate, setEsr]);
}
