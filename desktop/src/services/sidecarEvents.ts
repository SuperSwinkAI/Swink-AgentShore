import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export interface SidecarCrashedPayload {
  exit_code: number | null;
  last_stderr_lines: string[];
  log_file_path: string | null;
}

export async function subscribeSidecarCrashed(
  handler: (payload: SidecarCrashedPayload) => void,
): Promise<UnlistenFn> {
  return listen<SidecarCrashedPayload>("sidecar:crashed", (event) => {
    handler(event.payload);
  });
}

/**
 * Generic sidecar JSON-RPC notification forwarded by the Rust
 * supervisor. The supervisor reads every line the sidecar prints to
 * stdout, splits responses (matched by id) from notifications, and
 * re-emits notifications under this event name with the original
 * ``method`` + ``params`` envelope intact.
 */
export interface SidecarNotificationPayload {
  method: string;
  params: unknown;
}

export async function subscribeSidecarNotification(
  handler: (payload: SidecarNotificationPayload) => void,
): Promise<UnlistenFn> {
  return listen<SidecarNotificationPayload>("sidecar:notification", (event) => {
    handler(event.payload);
  });
}

/**
 * Subset of ``$/progress`` notification params used by the desktop
 * shell's Starting Progress screen.
 *
 * The wire shape from the sidecar matches DESIGN §2.4:
 * ``{token, step, percent, message}`` with an optional ``error``
 * string the sidecar adds for the failure case. ``status`` is *not*
 * emitted on the wire — we derive it from ``percent`` and ``error``
 * so React screens keep their simple ``"running" | "ok" | "failed"``
 * API for ``applyProgressEvent``.
 */
export interface ProgressNotificationParams {
  token?: string | number;
  step?: string;
  status?: "running" | "ok" | "failed";
  percent?: number;
  message?: string;
  error?: string | null;
}

function deriveStatus(
  percent: number | undefined,
  error: string | null,
): "running" | "ok" | "failed" | undefined {
  if (error !== null) return "failed";
  if (percent === undefined) return undefined;
  if (percent >= 100) return "ok";
  if (percent >= 0) return "running";
  return undefined;
}

/**
 * Subscribe to ``$/progress`` notifications routed through
 * ``sidecar.notification``. Returns an unlisten function.
 *
 * Parses the DESIGN §2.4 wire shape and derives an additional
 * ``status`` field so the StartingProgress screen can map each event
 * directly into ``applyProgressEvent``. Notifications for any other
 * method are ignored here — use ``subscribeSidecarNotification`` for
 * the raw stream.
 */
export async function subscribeProgress(
  handler: (params: ProgressNotificationParams) => void,
): Promise<UnlistenFn> {
  return subscribeSidecarNotification((payload) => {
    if (payload.method !== "$/progress") {
      return;
    }
    const raw = (payload.params ?? {}) as Record<string, unknown>;
    const token =
      typeof raw.token === "string" || typeof raw.token === "number" ? raw.token : undefined;
    const step = typeof raw.step === "string" ? raw.step : undefined;
    const percent = typeof raw.percent === "number" ? raw.percent : undefined;
    const message = typeof raw.message === "string" ? raw.message : undefined;
    const error = typeof raw.error === "string" ? raw.error : null;
    handler({
      token,
      step,
      status: deriveStatus(percent, error),
      percent,
      message,
      error,
    });
  });
}
