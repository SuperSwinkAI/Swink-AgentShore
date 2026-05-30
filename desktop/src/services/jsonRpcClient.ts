import { callJsonRpc as callViaBridge } from "../rpc/jsonrpc";

/**
 * Forward a JSON-RPC call to the sidecar via the Tauri bridge.
 */
export async function callJsonRpc<T>(method: string, params?: unknown): Promise<T> {
  return callViaBridge<T>(method, params);
}
