import { describe, expect, it, vi } from "vitest";

const callJsonRpc = vi.fn();
vi.mock("./jsonrpc", () => ({
  callJsonRpc: (method: string, params?: unknown) => callJsonRpc(method, params),
}));

import { setSeedPaths, setTargetBranch, setTrustedIssueEnforcement } from "./projectClient";

describe("projectClient.setSeedPaths", () => {
  it("posts project.set_seed_paths with the seed_paths array", async () => {
    callJsonRpc.mockResolvedValueOnce({ seed_paths: ["docs/PRD.md"], yaml_path: "/p/agentshore.yaml" });
    await setSeedPaths(["docs/PRD.md"]);
    expect(callJsonRpc).toHaveBeenCalledWith("project.set_seed_paths", {
      seed_paths: ["docs/PRD.md"],
    });
  });

  it("sends an empty array to clear the configured seed", async () => {
    callJsonRpc.mockResolvedValueOnce({ seed_paths: [], yaml_path: "/p/agentshore.yaml" });
    await setSeedPaths([]);
    expect(callJsonRpc).toHaveBeenCalledWith("project.set_seed_paths", { seed_paths: [] });
  });

  it("setTargetBranch still posts its own method (regression guard)", async () => {
    callJsonRpc.mockResolvedValueOnce({ target_branch: "integration" });
    await setTargetBranch("integration");
    expect(callJsonRpc).toHaveBeenCalledWith("project.set_target_branch", { name: "integration" });
  });
});

describe("projectClient.setTrustedIssueEnforcement", () => {
  it("posts project.set_trusted_issue_enforcement with the enabled flag", async () => {
    callJsonRpc.mockResolvedValueOnce({ enabled: true, yaml_path: "/p/agentshore.yaml" });
    await setTrustedIssueEnforcement(true);
    expect(callJsonRpc).toHaveBeenCalledWith("project.set_trusted_issue_enforcement", {
      enabled: true,
    });
  });
});
