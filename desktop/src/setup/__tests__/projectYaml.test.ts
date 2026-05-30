import { describe, expect, it } from "vitest";

import {
  budgetHydrationToSelection,
  budgetSelectionToConfig,
  parseProjectYaml,
} from "../projectYaml";

const FULL_YAML = `agents:
  claude_code:
    binary: claude
    enabled: true
    model: sonnet
  codex:
    binary: codex
    enabled: true
    model: gpt-5.3-codex
  gemini:
    binary: gemini
    enabled: false
github:
  repo: example-user/example-repo
identities:
  example-user:
    git_user_name: example-user
  unseriousai:
    git_user_name: unseriousai
project:
  goals: null
  path: .
  target_branch: trunk
`;

describe("parseProjectYaml", () => {
  it("returns empty hydration for null / empty input", () => {
    expect(parseProjectYaml(null)).toEqual({
      targetBranch: null,
      enabledAgents: [],
      identityLogins: [],
      budget: null,
    });
    expect(parseProjectYaml("")).toEqual({
      targetBranch: null,
      enabledAgents: [],
      identityLogins: [],
      budget: null,
    });
    expect(parseProjectYaml("   \n\n  ")).toEqual({
      targetBranch: null,
      enabledAgents: [],
      identityLogins: [],
      budget: null,
    });
  });

  it("extracts target_branch from project block", () => {
    expect(parseProjectYaml(FULL_YAML).targetBranch).toBe("trunk");
  });

  it("collects only enabled: true agents", () => {
    const r = parseProjectYaml(FULL_YAML);
    expect(r.enabledAgents.sort()).toEqual(["claude_code", "codex"]);
    expect(r.enabledAgents).not.toContain("gemini");
  });

  it("collects identity logins from the identities mapping", () => {
    const r = parseProjectYaml(FULL_YAML);
    expect(r.identityLogins.sort()).toEqual(["example-user", "unseriousai"]);
  });

  it("ignores nested keys that look like top-level keys", () => {
    const yaml = `agents:
  codex:
    binary: codex
    enabled: true
    project:
      target_branch: should-not-pick-this
project:
  target_branch: real-branch
`;
    expect(parseProjectYaml(yaml).targetBranch).toBe("real-branch");
  });

  it("strips inline comments", () => {
    const yaml = `project:
  target_branch: develop  # comment after value
identities:
  example-user:  # inline
    git_user_name: example-user
`;
    const r = parseProjectYaml(yaml);
    expect(r.targetBranch).toBe("develop");
    expect(r.identityLogins).toEqual(["example-user"]);
  });

  it("handles quoted scalar values", () => {
    const yaml = `project:
  target_branch: "main"
identities:
  example-user:
    git_user_name: 'example-user'
`;
    expect(parseProjectYaml(yaml).targetBranch).toBe("main");
  });

  it("returns empty when sections are absent", () => {
    const yaml = `github:
  repo: foo/bar
rl:
  policy_mode: learning
`;
    expect(parseProjectYaml(yaml)).toEqual({
      targetBranch: null,
      enabledAgents: [],
      identityLogins: [],
      budget: null,
    });
  });

  it("does not duplicate agent or identity entries", () => {
    // Pathological — same key twice — should still produce one entry.
    const yaml = `identities:
  example-user:
    git_user_name: x
  example-user:
    git_user_name: y
agents:
  codex:
    enabled: true
  codex:
    enabled: true
`;
    const r = parseProjectYaml(yaml);
    expect(r.identityLogins).toEqual(["example-user"]);
    expect(r.enabledAgents).toEqual(["codex"]);
  });

  it("parses budget.enabled and budget.total when present", () => {
    const yaml = `budget:
  enabled: true
  total: 250.0
  warning_threshold: 0.2
project:
  target_branch: main
`;
    const r = parseProjectYaml(yaml);
    expect(r.budget).toEqual({ enabled: true, totalUsd: 250 });
  });

  it("returns enabled=false with no total when budget block has only enabled:false", () => {
    const yaml = `budget:
  enabled: false
`;
    const r = parseProjectYaml(yaml);
    expect(r.budget).toEqual({ enabled: false, totalUsd: null });
  });

  it("ignores budget.warning_threshold and other unknown fields", () => {
    const yaml = `budget:
  warning_threshold: 0.5
`;
    // Only warning_threshold present, no enabled/total → treated as absent.
    expect(parseProjectYaml(yaml).budget).toBeNull();
  });

  it("treats malformed lines as no-ops rather than throwing", () => {
    const yaml = `: leading colon
not a key value
project:
  target_branch: ok
  # full-line comment
- a list item that should be skipped
identities:
  example-user:
    git_user_name: x
`;
    expect(() => parseProjectYaml(yaml)).not.toThrow();
    const r = parseProjectYaml(yaml);
    expect(r.targetBranch).toBe("ok");
    expect(r.identityLogins).toEqual(["example-user"]);
  });
});

describe("budgetHydrationToSelection", () => {
  it("returns null when hydration is null", () => {
    expect(budgetHydrationToSelection(null)).toBeNull();
  });

  it("maps enabled=true to mode='capped' with totalUsd", () => {
    expect(budgetHydrationToSelection({ enabled: true, totalUsd: 300 })).toEqual({
      mode: "capped",
      total: 300,
    });
  });

  it("maps enabled=false to mode='unlimited' regardless of totalUsd", () => {
    expect(budgetHydrationToSelection({ enabled: false, totalUsd: null })).toEqual({
      mode: "unlimited",
      total: 0,
    });
    expect(budgetHydrationToSelection({ enabled: false, totalUsd: 50 })).toEqual({
      mode: "unlimited",
      total: 0,
    });
  });

  it("falls back to total=0 when capped hydration has no totalUsd", () => {
    expect(budgetHydrationToSelection({ enabled: true, totalUsd: null })).toEqual({
      mode: "capped",
      total: 0,
    });
  });
});

describe("budgetSelectionToConfig", () => {
  it("serializes capped mode to enabled=true with the dollar total", () => {
    expect(budgetSelectionToConfig({ mode: "capped", total: 250 })).toEqual({
      enabled: true,
      total: 250,
    });
  });

  it("serializes unlimited mode to enabled=false with total=0", () => {
    expect(budgetSelectionToConfig({ mode: "unlimited", total: 250 })).toEqual({
      enabled: false,
      total: 0,
    });
  });
});
