import { defineConfig, devices } from "@playwright/test";

// Opt-in gate: the Playwright suite is heavy (browser launch + dev-server + mock
// WS) and only meaningful when verifying UI integrity. Default invocations of
// `playwright test` or `npm run test:e2e` match zero specs; the explicit
// `npm run ui-integrity-check` sets UI_INTEGRITY_CHECK=1 and runs the suite.
const enabled = process.env.UI_INTEGRITY_CHECK === "1";

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: enabled ? "**/*.spec.ts" : [],
  timeout: 30_000,
  fullyParallel: true,
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
  },
  webServer: enabled
    ? [
        {
          command:
            "AGENTSHORE_MOCK_PORT=9473 node tests/e2e/mockServer.mjs",
          url: "http://127.0.0.1:9473/health",
          reuseExistingServer: false,
        },
        {
          command:
            "AGENTSHORE_DASHBOARD_WS_TARGET=ws://127.0.0.1:9473 npm run dev -- --host 127.0.0.1",
          url: "http://127.0.0.1:5173",
          reuseExistingServer: !process.env.CI,
        },
      ]
    : undefined,
  projects: [
    {
      name: "desktop",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1280, height: 800 },
      },
    },
  ],
});
