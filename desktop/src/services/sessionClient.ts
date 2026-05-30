import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { callJsonRpc } from "./jsonRpcClient";
import type { EsrPayload } from "./sessionContext";

export interface StartSessionParams {
  /**
   * Optional opaque token echoed back on every ``$/progress`` notification
   * so the progress-listener UI can correlate events to this call (DESIGN
   * §2.4, §10.2).
   */
  progressToken?: string | number;
  /** Optional seed file or folder for the first seed_project play. */
  seedInputPath?: string | null;
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
  return callJsonRpc<StartSessionResult>("session.start", rpcParams);
}

export interface StopSessionParams {
  mode?: string;
  progressToken?: string | number;
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
export function subscribeCompleted(
  handler: (payload: EsrPayload) => void,
): () => void {
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
