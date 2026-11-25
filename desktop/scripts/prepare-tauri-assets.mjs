#!/usr/bin/env node





import { spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  rmSync,
  statSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const desktopDir = resolve(__dirname, "..");
const repoRoot = resolve(desktopDir, "..");
const dashboardDir = resolve(repoRoot, "dashboard");
const dashboardStaticDir = resolve(
  repoRoot,
  "src",
  "agentshore",
  "dashboard",
  "static",
);
const desktopDistDashboardDir = resolve(desktopDir, "dist", "dashboard");

function run(cmd, args, cwd) {
  const result = spawnSync(cmd, args, {
    cwd,
    stdio: "inherit",
    shell: process.platform === "win32",
  });
  if (result.status !== 0) {
    throw new Error(
      `Command failed (${result.status}): ${cmd} ${args.join(" ")} (cwd=${cwd})`,
    );
  }
}

function isDirectory(path) {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}








console.log("[prepare-tauri-assets] building @agentshore/dashboard (bridge static)");
run("npm", ["run", "build"], dashboardDir);
console.log("[prepare-tauri-assets] building @agentshore/dashboard (lib bundle)");
run("npm", ["run", "build:lib"], dashboardDir);

if (!isDirectory(dashboardStaticDir)) {
  throw new Error(
    `dashboard build did not produce ${dashboardStaticDir}; refusing to continue`,
  );
}

console.log(
  `[prepare-tauri-assets] staging ${dashboardStaticDir} -> ${desktopDistDashboardDir}`,
);
if (existsSync(desktopDistDashboardDir)) {
  rmSync(desktopDistDashboardDir, { recursive: true, force: true });
}
mkdirSync(dirname(desktopDistDashboardDir), { recursive: true });
cpSync(dashboardStaticDir, desktopDistDashboardDir, { recursive: true });

console.log("[prepare-tauri-assets] done");
