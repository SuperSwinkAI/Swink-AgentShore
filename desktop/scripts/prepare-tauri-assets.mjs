#!/usr/bin/env node
// Build the dashboard package once and stage its assets where Tauri can bundle
// them. The dashboard build emits into the Python package's static dir
// (src/agentshore/dashboard/static), which keeps `agentshore dashboard` working.
// We then mirror that tree into desktop/dist/dashboard so the WebView can
// load it at tauri://localhost/dashboard/... offline.
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

// The desktop Vite build consumes @agentshore/dashboard as a workspace lib.
// Sprite PNGs are emitted by the desktop Vite build itself (via Rollup's
// emitFile / new URL(path, import.meta.url) pattern in the lib code).
// This script stages the bridge-static build into desktop/dist/dashboard/
// for Tauri to embed. Run both builds; lib second so dist/ ends in the
// bundled-lib state (bridge `npm run build` leaves raw tsc output in dist/
// that the desktop vite build cannot resolve — see desktop-rbn).
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
