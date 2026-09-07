import { readFileSync } from "fs";
import { resolve } from "path";

import { describe, expect, it } from "vitest";

const DASHBOARD_DIR = process.cwd();

function source(path: string): string {
  return readFileSync(resolve(DASHBOARD_DIR, path), "utf-8");
}

describe("dashboard style ownership", () => {
  it.each([
    ["src/components/Dashboard.tsx", './Dashboard.css'],
    ["src/components/EventDrawer.tsx", './EventDrawer.css'],
    ["src/components/SidePanel.tsx", './SidePanel.css'],
    ["src/components/PlayBar.tsx", './PlayBar.css'],
    ["src/components/EpicPanel.tsx", './EpicPanel.css'],
    ["src/components/FeedbackModal.tsx", './Modal.css'],
    ["src/components/BootstrapModal.tsx", './Modal.css'],
    ["src/components/PlaysPanel.tsx", './PlaysPanel.css'],
    ["src/components/StageTabs.tsx", './StageTabs.css'],
    ["src/components/StatsStage.tsx", './StatsStage.css'],
    ["src/components/KanbanStage.tsx", './KanbanStage.css'],
    ["src/components/ErrorBoundary.tsx", './ErrorBoundary.css'],
    ["src/components/kanban/IssueDetailModal.tsx", './IssueDetailModal.css'],
  ])("%s imports its co-located stylesheet", (component, stylesheet) => {
    expect(source(component)).toContain(`import "${stylesheet}";`);
  });

  it("publishes one shared theme contract from the dashboard package", () => {
    const dashboardShell = source("src/components/Dashboard.css");
    const theme = source("src/styles/theme.css");
    const desktop = source("../desktop/src/styles.css");

    expect(dashboardShell).toContain('@import "../styles/theme.css";');
    expect(theme).toContain("--color-fm-bg:");
    expect(theme).toContain("--fm-button-primary-bg:");
    expect(desktop).not.toMatch(/:root[^}]*--color-fm-bg:/s);
  });
});
