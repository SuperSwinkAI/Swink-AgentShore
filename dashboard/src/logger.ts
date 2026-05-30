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
    // Errors are always surfaced — they indicate broken invariants users
    // and devs both need to see, and they're infrequent enough that rate
    // limiting hasn't been needed. If that changes, gate this behind a
    // token bucket here in one place.
    console.error("[agentshore-dashboard]", { channel, message, ...context });
  },
};
