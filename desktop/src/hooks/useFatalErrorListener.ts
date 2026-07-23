import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useState } from "react";
import type { NavigateFunction } from "react-router-dom";
import type { FatalShellInfo } from "../screens/FatalErrorScreen";

/**
 * DESIGN §2.6 fatal-error surface. Two ways the shell finds out the
 * supervisor failed:
 *  1. Tauri event `app.fatal_error` (emitted from the setup hook the
 *     instant the supervisor returns Err). Useful if the WebView is
 *     already mounted when the failure happens — rare in practice.
 *  2. `get_fatal_shell_state` Tauri command queried on mount. This is
 *     the primary path because the setup hook usually runs before the
 *     React app is ready to receive events.
 */
export function useFatalErrorListener(navigate: NavigateFunction): FatalShellInfo | null {
  const [fatalInfo, setFatalInfo] = useState<FatalShellInfo | null>(null);

  useEffect(() => {
    void invoke<FatalShellInfo | null>("get_fatal_shell_state")
      .then((info) => {
        if (info) {
          setFatalInfo(info);
          navigate("/fatal-error", { replace: true });
        }
      })
      .catch(() => undefined);

    let cancelled = false;
    let unlisten: (() => void) | null = null;
    void listen<FatalShellInfo>("app:fatal_error", (event) => {
      if (cancelled) return;
      setFatalInfo(event.payload);
      navigate("/fatal-error", { replace: true });
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

  return fatalInfo;
}
