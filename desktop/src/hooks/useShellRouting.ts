import { useEffect } from "react";
import type { NavigateFunction } from "react-router-dom";

/**
 * Match the bridge SPA's body chrome when the dashboard fills the
 * viewport: use dashboard.css's themed --color-fm-bg.
 */
export function useDashboardBodyClass(pathname: string): void {
  useEffect(() => {
    const dashboardRoutes = ["/session/dashboard", "/dashboard", "/demo"];
    const onDashboard = dashboardRoutes.some((route) => pathname.startsWith(route));
    document.body.classList.toggle("dashboard-active", onDashboard);
    return () => {
      document.body.classList.remove("dashboard-active");
    };
  }, [pathname]);
}

/**
 * Cmd+Shift+D from anywhere jumps to the demo dashboard (desktop-ooao).
 * Skip-setup mount for iterating on Dashboard UI without going through
 * Choose Project → Readiness → Identities → Agents → Start.
 */
export function useDemoDashboardShortcut(navigate: NavigateFunction): void {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (
        event.shiftKey &&
        (event.metaKey || event.ctrlKey) &&
        (event.key === "D" || event.key === "d")
      ) {
        event.preventDefault();
        navigate("/demo");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [navigate]);
}
