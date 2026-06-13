
import { afterEach, describe, expect, it, vi } from "vitest";
import { callJsonRpc, JsonRpcError, RPC_CLIENT_TIMEOUT_CODE } from "./jsonrpc";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(),
}));

const { invoke } = await import("@tauri-apps/api/core");

afterEach(() => {
  vi.useRealTimers();
  vi.mocked(invoke).mockReset();
});

describe("callJsonRpc", () => {
  it("invokes jsonrpc_call with method and params", async () => {
    vi.mocked(invoke).mockResolvedValueOnce([{"id": "x"}]);
    const result = await callJsonRpc<unknown[]>("recents.list");
    expect(result).toEqual([{ id: "x" }]);
    expect(invoke).toHaveBeenCalledWith("jsonrpc_call", {
      method: "recents.list",
      params: undefined,
    });
  });

  it("throws JsonRpcError for error envelope", async () => {
    vi.mocked(invoke).mockResolvedValueOnce({
      error: { code: -32601, message: "unknown method" },
    });
    await expect(callJsonRpc("missing.method")).rejects.toBeInstanceOf(JsonRpcError);
  });

  it("rejects with a timeout JsonRpcError when the bridge never responds", async () => {
    vi.useFakeTimers();
    // A bridge call that never settles — the backstop timeout must fire.
    vi.mocked(invoke).mockReturnValueOnce(new Promise<unknown>(() => {}));
    const promise = callJsonRpc("project.inspect");
    void promise.catch(() => undefined);
    await vi.advanceTimersByTimeAsync(36_000);
    await expect(promise).rejects.toMatchObject({ code: RPC_CLIENT_TIMEOUT_CODE });
  });

  it("does not cap long-running lifecycle methods", async () => {
    vi.useFakeTimers();
    let resolveCall: (value: unknown) => void = () => undefined;
    vi.mocked(invoke).mockReturnValueOnce(
      new Promise<unknown>((resolve) => {
        resolveCall = resolve;
      }),
    );
    const promise = callJsonRpc<{ session_id: string }>("session.start");
    // Advance far past any setup/default cap; session.start must NOT time out.
    await vi.advanceTimersByTimeAsync(600_000);
    resolveCall({ session_id: "s1" });
    await expect(promise).resolves.toEqual({ session_id: "s1" });
  });
});
