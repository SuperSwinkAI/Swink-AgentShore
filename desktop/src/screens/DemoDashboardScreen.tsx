import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import { Dashboard, createDemoTransport, type DemoScenario } from "agentshore-dashboard";

const SCENARIOS: ReadonlyArray<DemoScenario> = [
  "active",
  "empty",
  "feedback",
  "disconnected",
  "stress",
  "bootstrap",
];

/** Skip-setup demo mount for iterating on Dashboard UI inside the desktop app.
 *
 * Routed at /demo (also reachable via the Cmd+Shift+D shortcut wired in
 * App.tsx). Mirrors the bridge SPA's ?demo=1 entry point — the same
 * createDemoTransport from agentshore-dashboard drives a deterministic mock
 * AgentShore session so there's no need to walk through Choose Project →
 * Readiness → Target Branch → Identities → Agents → Start.
 *
 * Scenario can be switched via the ?scenario= query parameter; "active"
 * is the default. This route intentionally renders no extra desktop demo
 * chrome so it stays visually identical to the real session dashboard.
 */
export function DemoDashboardScreen() {
  const [searchParams] = useSearchParams();
  const scenarioParam = searchParams.get("scenario");
  const scenario: DemoScenario = SCENARIOS.includes(scenarioParam as DemoScenario)
    ? (scenarioParam as DemoScenario)
    : "active";

  const transport = useMemo(() => {
    const params = new URLSearchParams();
    params.set("scenario", scenario);
    if (searchParams.has("freeze")) params.set("freeze", searchParams.get("freeze") ?? "1");
    return createDemoTransport(params);
  }, [scenario, searchParams]);

  return <Dashboard transport={transport} />;
}
