export type DashboardLogContext = Record<string, unknown>;

function isDebugEnabled(): boolean {
  if (import.meta.env.DEV) return true;
  if (typeof window === "undefined") return false;

  const debug = new URLSearchParams(window.location.search).get("debug");
  return debug === "1" || debug === "true" || debug === "dashboard";
}

export function errorContext(error: unknown): DashboardLogContext {
  if (error instanceof Error) {
    return { error: { name: error.name, message: error.message } };
  }
  return { error: String(error) };
}

export const dashboardLogger = {
  warn(
    channel: string,
    message: string,
    context: DashboardLogContext = {},
  ): void {
    if (!isDebugEnabled()) return;
    console.warn("[agentshore-dashboard]", { channel, message, ...context });
  },
  error(
    channel: string,
    message: string,
    context: DashboardLogContext = {},
  ): void {

    console.error("[agentshore-dashboard]", { channel, message, ...context });
  },
};
