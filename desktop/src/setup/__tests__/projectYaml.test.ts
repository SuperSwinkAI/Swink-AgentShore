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
      timelapse: null,
      trustedIssueEnforcement: null,
    });
    expect(parseProjectYaml("")).toEqual({
      targetBranch: null,
      enabledAgents: [],
      identityLogins: [],
      budget: null,
      timelapse: null,
      trustedIssueEnforcement: null,
    });
    expect(parseProjectYaml("   \n\n  ")).toEqual({
      targetBranch: null,
      enabledAgents: [],
      identityLogins: [],
      budget: null,
      timelapse: null,
      trustedIssueEnforcement: null,
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
      timelapse: null,
      trustedIssueEnforcement: null,
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
    expect(r.budget).toEqual({
      enabled: true,
      totalUsd: 250,
      timeEnabled: false,
      timeMinutes: null,
    });
  });

  it("returns enabled=false with no total when budget block has only enabled:false", () => {
    const yaml = `budget:
  enabled: false
`;
    const r = parseProjectYaml(yaml);
    expect(r.budget).toEqual({
      enabled: false,
      totalUsd: null,
      timeEnabled: false,
      timeMinutes: null,
    });
  });

  it("parses the budget time cap when present", () => {
    const yaml = `budget:
  enabled: true
  total: 100.0
  time_enabled: true
  time_total_minutes: 1440
`;
    const r = parseProjectYaml(yaml);
    expect(r.budget).toEqual({
      enabled: true,
      totalUsd: 100,
      timeEnabled: true,
      timeMinutes: 1440,
    });
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

describe("parseProjectYaml — trusted_ids.restrict_issues_to_trusted_authors", () => {
  it("parses true from the trusted_ids block", () => {
    const yaml = `trusted_ids:
  github_logins:
    - example-user
  restrict_issues_to_trusted_authors: true
`;
    expect(parseProjectYaml(yaml).trustedIssueEnforcement).toBe(true);
  });

  it("parses false from the trusted_ids block", () => {
    const yaml = `trusted_ids:
  restrict_issues_to_trusted_authors: false
`;
    expect(parseProjectYaml(yaml).trustedIssueEnforcement).toBe(false);
  });

  it("leaves trustedIssueEnforcement null when the key is absent", () => {
    const yaml = `trusted_ids:
  github_logins:
    - example-user
`;
    expect(parseProjectYaml(yaml).trustedIssueEnforcement).toBeNull();
    expect(parseProjectYaml(FULL_YAML).trustedIssueEnforcement).toBeNull();
  });
});

describe("budgetHydrationToSelection", () => {
  it("returns null when hydration is null", () => {
    expect(budgetHydrationToSelection(null)).toBeNull();
  });

  it("maps enabled=true to mode='capped' with totalUsd", () => {
    expect(
      budgetHydrationToSelection({
        enabled: true,
        totalUsd: 300,
        timeEnabled: false,
        timeMinutes: null,
      }),
    ).toEqual({ mode: "capped", total: 300, timeMode: "unlimited", timeMinutes: 0 });
  });

  it("maps enabled=false to mode='unlimited' regardless of totalUsd", () => {
    expect(
      budgetHydrationToSelection({
        enabled: false,
        totalUsd: null,
        timeEnabled: false,
        timeMinutes: null,
      }),
    ).toEqual({ mode: "unlimited", total: 0, timeMode: "unlimited", timeMinutes: 0 });
    expect(
      budgetHydrationToSelection({
        enabled: false,
        totalUsd: 50,
        timeEnabled: false,
        timeMinutes: null,
      }),
    ).toEqual({ mode: "unlimited", total: 0, timeMode: "unlimited", timeMinutes: 0 });
  });

  it("falls back to total=0 when capped hydration has no totalUsd", () => {
    expect(
      budgetHydrationToSelection({
        enabled: true,
        totalUsd: null,
        timeEnabled: false,
        timeMinutes: null,
      }),
    ).toEqual({ mode: "capped", total: 0, timeMode: "unlimited", timeMinutes: 0 });
  });

  it("maps the time dimension independently", () => {
    expect(
      budgetHydrationToSelection({
        enabled: false,
        totalUsd: null,
        timeEnabled: true,
        timeMinutes: 1440,
      }),
    ).toEqual({ mode: "unlimited", total: 0, timeMode: "capped", timeMinutes: 1440 });
  });
});

describe("parseProjectYaml — timelapse", () => {
  it("parses the timelapse block", () => {
    const result = parseProjectYaml("timelapse:\n  enabled: true\n  installed: true\n");
    expect(result.timelapse).toEqual({ enabled: true, installed: true });
  });

  it("leaves timelapse null when the block is absent", () => {
    expect(parseProjectYaml("project:\n  path: .\n").timelapse).toBeNull();
  });

  it("parses installed independently of enabled", () => {
    const result = parseProjectYaml("timelapse:\n  installed: true\n");
    expect(result.timelapse).toEqual({ enabled: false, installed: true });
  });
});

describe("budgetSelectionToConfig", () => {
  it("serializes capped mode to enabled=true with the dollar total", () => {
    expect(budgetSelectionToConfig({ mode: "capped", total: 250 })).toEqual({
      enabled: true,
      total: 250,
      time_enabled: false,
      time_total_minutes: 0,
    });
  });

  it("serializes unlimited mode to enabled=false with total=0", () => {
    expect(budgetSelectionToConfig({ mode: "unlimited", total: 250 })).toEqual({
      enabled: false,
      total: 0,
      time_enabled: false,
      time_total_minutes: 0,
    });
  });

  it("serializes the time dimension independently", () => {
    expect(
      budgetSelectionToConfig({
        mode: "capped",
        total: 250,
        timeMode: "capped",
        timeMinutes: 1440,
      }),
    ).toEqual({ enabled: true, total: 250, time_enabled: true, time_total_minutes: 1440 });
  });

  it("treats capped dollars + unlimited time as time disabled", () => {
    expect(
      budgetSelectionToConfig({
        mode: "capped",
        total: 250,
        timeMode: "unlimited",
        timeMinutes: 1440,
      }),
    ).toEqual({ enabled: true, total: 250, time_enabled: false, time_total_minutes: 0 });
  });
});
