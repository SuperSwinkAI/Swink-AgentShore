import { invoke } from "@tauri-apps/api/core";
// Single source of truth for RPC method timeout classes.
// The Rust supervisor (sidecar.rs:response_timeout_for_method) reads the same
// file via include_str! — do not edit method lists here; edit
// desktop/rpc-method-classes.json instead.
import methodClasses from "../../rpc-method-classes.json";

export interface JsonRpcErrorPayload {
  code: number;
  message: string;
  data?: unknown;
}

/** JSON-RPC error code used when the frontend backstop timeout fires. */
export const RPC_CLIENT_TIMEOUT_CODE = -32001;

/**
 * Per-method-class frontend timeout (ms), deliberately set slightly ABOVE the
 * Rust supervisor's per-method ``recv_timeout`` so the backend's diagnostic
 * error normally wins the race. This is a backstop for the case where the Tauri
 * bridge itself never returns (so the screen shows a clear error instead of an
 * infinite spinner). Long-running lifecycle calls are uncapped — they report
 * progress via ``$/progress`` and have their own overlay/timeout handling.
 *
 * Method lists are read from desktop/rpc-method-classes.json (single source of
 * truth shared with the Rust supervisor).
 */
const SETUP_TIMEOUT_MS = 35_000; // Rust SETUP_RESPONSE_TIMEOUT is 30s.
const DEFAULT_TIMEOUT_MS = 130_000; // Rust RESPONSE_TIMEOUT is 120s.

const SETUP_METHODS: ReadonlySet<string> = new Set(methodClasses.setup);
const UNCAPPED_METHODS: ReadonlySet<string> = new Set(methodClasses.uncapped);

function timeoutForMethod(method: string): number | null {
  if (UNCAPPED_METHODS.has(method)) return null;
  if (SETUP_METHODS.has(method)) return SETUP_TIMEOUT_MS;
  return DEFAULT_TIMEOUT_MS;
}

class RpcTimeout extends Error {
  constructor(
    readonly method: string,
    readonly elapsedMs: number,
  ) {
    super(`${method} did not respond within ${Math.round(elapsedMs / 1000)}s`);
    this.name = "RpcTimeout";
  }
}

export async function withTimeout<T>(promise: Promise<T>, method: string, ms: number): Promise<T> {
  const started = Date.now();
  let handle: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    handle = setTimeout(() => reject(new RpcTimeout(method, Date.now() - started)), ms);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    if (handle !== undefined) clearTimeout(handle);
  }
}

export class JsonRpcError extends Error {
  readonly code: number;
  readonly data?: unknown;

  constructor(payload: JsonRpcErrorPayload) {
    super(payload.message);
    this.name = "JsonRpcError";
    this.code = payload.code;
    this.data = payload.data;
  }
}

function isErrorEnvelope(value: unknown): value is { error: JsonRpcErrorPayload } {
  if (typeof value !== "object" || value === null || !("error" in value)) {
    return false;
  }
  const err = (value as { error: unknown }).error;
  return (
    typeof err === "object" &&
    err !== null &&
    typeof (err as { code?: unknown }).code === "number" &&
    typeof (err as { message?: unknown }).message === "string"
  );
}

function isResultEnvelope(value: unknown): value is { result: unknown } {
  return typeof value === "object" && value !== null && "result" in value;
}

export async function callJsonRpc<T>(method: string, params?: unknown): Promise<T> {
  let raw: unknown;
  try {
    const call = invoke<unknown>("jsonrpc_call", { method, params });
    const timeoutMs = timeoutForMethod(method);
    raw = timeoutMs === null ? await call : await withTimeout(call, method, timeoutMs);
  } catch (err) {
    if (err instanceof JsonRpcError) {
      throw err;
    }
    if (err instanceof RpcTimeout) {
      throw new JsonRpcError({ code: RPC_CLIENT_TIMEOUT_CODE, message: err.message });
    }
    if (typeof err === "object" && err !== null && isErrorEnvelope(err)) {
      throw new JsonRpcError(err.error);
    }
    throw new JsonRpcError({
      code: -32603,
      message: err instanceof Error ? err.message : String(err),
    });
  }

  if (isErrorEnvelope(raw)) {
    throw new JsonRpcError(raw.error);
  }
  if (isResultEnvelope(raw)) {
    return raw.result as T;
  }
  return raw as T;
}
