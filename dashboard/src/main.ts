import { createElement } from "react";
import { createRoot } from "react-dom/client";

import { Dashboard } from "./components/Dashboard";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { createDemoTransport } from "./demoTransport";
import { getOrCreateReactRoot } from "./reactEntry";

/**
 * Bridge-served SPA entry point. The desktop app mounts <Dashboard/>
 * directly inside SessionDashboardScreen; this entry mirrors that mount
 * for the standalone bridge URL so both surfaces share one React tree
 * (desktop-77cj).
 */
const params = new URLSearchParams(window.location.search);
const demoParam = params.get("demo");
const useDemoTransport =
  demoParam === "1" || (demoParam !== "0" && import.meta.env.DEV);

const wsUrl = `ws://${window.location.host}/ws`;

const root = createRoot(getOrCreateReactRoot());
root.render(
  createElement(
    ErrorBoundary,
    null,
    createElement(Dashboard, {
      wsUrl: useDemoTransport ? undefined : wsUrl,
      transport: useDemoTransport ? createDemoTransport(params) : undefined,
    }),
  ),
);
