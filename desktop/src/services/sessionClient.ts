/**
 * Re-exports from ``rpc/sessionClient`` for backward-compat.
 *
 * The session lifecycle helpers (startSession, stopSession, subscribeCompleted)
 * have been merged into ``rpc/sessionClient.ts``. Existing imports from
 * ``services/sessionClient`` continue to work via this re-export barrel.
 */
export {
  startSession,
  stopSession,
  subscribeCompleted,
  type StartSessionParams,
  type StartSessionResult,
  type StopSessionParams,
  type IpcEndpoint,
} from "../rpc/sessionClient";
