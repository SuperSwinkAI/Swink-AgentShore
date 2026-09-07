import { beforeEach, describe, expect, it, vi } from "vitest";

const callJsonRpc = vi.fn();

vi.mock("./jsonrpc", () => ({
  callJsonRpc: (method: string, params?: unknown) => callJsonRpc(method, params),
}));

import { checkAgentAuth } from "./agentsClient";

describe("agentsClient", () => {
  beforeEach(() => {
    callJsonRpc.mockReset();
  });

  it("probes configured CLI backend authentication through agents.check_auth", async () => {
    const rows = [
      {
        agent_type: "codex",
        status: "ok",
        detail: "Logged in using ChatGPT",
      },
    ];
    callJsonRpc.mockResolvedValueOnce(rows);

    await expect(checkAgentAuth()).resolves.toEqual(rows);
    expect(callJsonRpc).toHaveBeenCalledWith("agents.check_auth", undefined);
  });
});
