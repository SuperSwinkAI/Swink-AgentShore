/**
 * Session RPC client — both live-budget calls (``session.set_budget`` /
 * ``session.get_budget``) and lifecycle calls (``session.start`` /
 * ``session.stop`` / ``subscribeCompleted``).
 *
 * Merged from the former ``services/sessionClient.ts`` (lifecycle) and
 * ``rpc/sessionClient.ts`` (budget). Budget types and helpers live in
 * ``rpc/budget.ts``; this file re-exports what consumers need so existing
 * imports continue to resolve.
 */

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { callJsonRpc } from "./jsonrpc";
import type { AppliedBudget, LiveBudgetInput } from "./budget";
import type { EsrPayload } from "../services/sessionContext";

// ---------------------------------------------------------------------------
// Re-exports for backward-compat (consumers imported from either location)
// ---------------------------------------------------------------------------
export type { AppliedBudget, LiveBudgetInput };
/** @deprecated Use {@link LiveBudgetInput} — this alias will be removed. */
export type BudgetRpcInput = LiveBudgetInput;

export interface BudgetRpcResult {
  budget: AppliedBudget;
}

// ---------------------------------------------------------------------------
// Budget RPC
// ---------------------------------------------------------------------------

/** Apply a new budget to the running session (``session.set_budget``). */
export async function setBudgetLive(budget: LiveBudgetInput): Promise<BudgetRpcResult> {
  return callJsonRpc<BudgetRpcResult>("session.set_budget", { budget });
}

/** Read the running session's current budget (``session.get_budget``). */
export async function getBudget(): Promise<BudgetRpcResult> {
  return callJsonRpc<BudgetRpcResult>("session.get_budget");
}

// ---------------------------------------------------------------------------
// Session lifecycle (merged from services/sessionClient.ts)
// ---------------------------------------------------------------------------

export interface StartSessionParams {
  /**
   * Optional opaque token echoed back on every ``$/progress`` notification
   * so the progress-listener UI can correlate events to this call.
   */
  progressToken?: string | number;
  /** Optional seed file or folder for the first seed_project play. */
  seedInputPath?: string | null;
  /**
   * Per-session override for the optional timelapse capture. When ``true``
   * (and the feature is installed) the sidecar records a dashboard timelapse
   * for this session.
   */
  timelapse?: boolean;
}

export interface IpcEndpoint {
  kind: string;
  host?: string;
  port?: number;
  /** Some bridge implementations also expose a ready-to-use URL. */
  url?: string;
  [key: string]: unknown;
}

export interface StartSessionResult {
  session_id: string;
  ipc_endpoint?: IpcEndpoint;
}

export async function startSession(
  params: StartSessionParams = {},
): Promise<StartSessionResult> {
  const rpcParams: Record<string, unknown> = {};
  if (params.progressToken !== undefined) {
    rpcParams.progress_token = params.progressToken;
  }
  if (params.seedInputPath !== undefined && params.seedInputPath !== null) {
    rpcParams.seed_input_path = params.seedInputPath;
  }
  if (params.timelapse !== undefined) {
    rpcParams.timelapse = params.timelapse;
  }
  return callJsonRpc<StartSessionResult>("session.start", rpcParams);
}

export interface StopSessionParams {
  mode?: string;
  progressToken?: string | number;
}

// ---------------------------------------------------------------------------
// Reattach — current session state
// ---------------------------------------------------------------------------

/**
 * Shape returned by the ``current_session`` Tauri command.
 * Fields are camelCase to match the Rust serde rename_all = "camelCase".
 */
export interface CurrentSessionInfo {
  active: boolean;
  dashboardUrl: string | null;
  sessionId: string | null;
}

/**
 * Query the Tauri host for the running session state.
 * Used on mount to reattach to a session that survived a WebView reload.
 */
export async function currentSession(): Promise<CurrentSessionInfo> {
  return invoke<CurrentSessionInfo>("current_session");
}

export async function stopSession(params: StopSessionParams = {}): Promise<EsrPayload> {
  const rpcParams: Record<string, unknown> = {};
  if (params.mode !== undefined) {
    rpcParams.mode = params.mode;
  }
  if (params.progressToken !== undefined) {
    rpcParams.progress_token = params.progressToken;
  }
  return callJsonRpc<EsrPayload>("session.stop", rpcParams);
}

/**
 * Subscribe to ``session.completed`` events forwarded by the Tauri bridge.
 *
 * The bridge re-emits the sidecar's JSON-RPC notification as a Tauri event so
 * React components can listen synchronously.
 */
export function subscribeCompleted(handler: (payload: EsrPayload) => void): () => void {
  let unlisten: UnlistenFn | null = null;
  let cancelled = false;
  void listen<EsrPayload>("session:completed", (event) => {
    handler(event.payload);
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
}
