
import { describe, expect, it, vi } from "vitest";
import { callJsonRpc, JsonRpcError } from "./jsonrpc";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(),
}));

const { invoke } = await import("@tauri-apps/api/core");

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
});
