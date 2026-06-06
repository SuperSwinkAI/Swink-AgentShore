import { describe, expect, it, vi } from "vitest";

const callJsonRpc = vi.fn();
vi.mock("./jsonrpc", () => ({
  callJsonRpc: (method: string, params?: unknown) =>
    callJsonRpc(method, params),
}));

import { getBudget, setBudgetLive } from "./sessionClient";

const APPLIED = {
  enabled: true,
  total: 250,
  spent: 10,
  remaining: 240,
  time_enabled: false,
  time_total_minutes: 0,
  time_elapsed_minutes: 5,
  time_remaining_minutes: 0,
};

describe("sessionClient.setBudgetLive", () => {
  it("posts session.set_budget with the budget payload", async () => {
    callJsonRpc.mockResolvedValueOnce({ budget: APPLIED });
    const result = await setBudgetLive({
      enabled: true,
      total: 250,
      time_enabled: false,
      time_total_minutes: 0,
    });
    expect(callJsonRpc).toHaveBeenCalledWith("session.set_budget", {
      budget: {
        enabled: true,
        total: 250,
        time_enabled: false,
        time_total_minutes: 0,
      },
    });
    expect(result.budget).toEqual(APPLIED);
  });
});

describe("sessionClient.getBudget", () => {
  it("calls session.get_budget with no params", async () => {
    callJsonRpc.mockResolvedValueOnce({ budget: APPLIED });
    const result = await getBudget();
    expect(callJsonRpc).toHaveBeenCalledWith("session.get_budget", undefined);
    expect(result.budget).toEqual(APPLIED);
  });
});
