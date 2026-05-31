import { describe, it, expect } from "vitest";
import { readFileSync } from "fs";
import { resolve } from "path";

// process.cwd() is the dashboard/ directory when vitest runs via `npm run test:unit`
const DASHBOARD_DIR = process.cwd();
const ROOT_PKG = JSON.parse(
  readFileSync(resolve(DASHBOARD_DIR, "../package.json"), "utf-8"),
);
const DASHBOARD_PKG = JSON.parse(
  readFileSync(resolve(DASHBOARD_DIR, "package.json"), "utf-8"),
);
const DESKTOP_PKG = JSON.parse(
  readFileSync(resolve(DASHBOARD_DIR, "../desktop/package.json"), "utf-8"),
);

const DASHBOARD_PACKAGE_NAME = "@agentshore/dashboard";

describe("workspace topology", () => {
  it("root workspaces include dashboard and desktop", () => {
    expect(ROOT_PKG.workspaces).toContain("dashboard");
    expect(ROOT_PKG.workspaces).toContain("desktop");
  });

  it("dashboard package uses the scoped workspace name", () => {
    expect(DASHBOARD_PKG.name).toBe(DASHBOARD_PACKAGE_NAME);
  });

  it("dashboard exports['.'].import ends in .js", () => {
    expect(DASHBOARD_PKG.exports["."].import).toMatch(/\.js$/);
  });

  it("dashboard exports['.'].types ends in .d.ts", () => {
    expect(DASHBOARD_PKG.exports["."].types).toMatch(/\.d\.ts$/);
  });

  it("desktop depends on @agentshore/dashboard as *", () => {
    const dep = DESKTOP_PKG.dependencies?.[DASHBOARD_PACKAGE_NAME];
    expect(dep).toBe("*");
    expect(DESKTOP_PKG.dependencies?.["agentshore-dashboard"]).toBeUndefined();
  });
});
