import { invoke } from "@tauri-apps/api/core";

export interface JsonRpcErrorPayload {
  code: number;
  message: string;
  data?: unknown;
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
    raw = await invoke<unknown>("jsonrpc_call", { method, params });
  } catch (err) {
    if (err instanceof JsonRpcError) {
      throw err;
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
