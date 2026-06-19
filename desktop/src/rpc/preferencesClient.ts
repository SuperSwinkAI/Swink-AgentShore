import { callJsonRpc } from "./jsonrpc";

/**
 * Machine-global preferences, as returned by the `preferences.*` sidecar RPCs.
 * `disableable_plays` is the full allowlist menu (every play the user is allowed
 * to turn off); `disabled_plays` is the subset currently off. Both are
 * `PlayType` value strings (e.g. `"run_qa"`).
 */
export interface PreferencesData {
  disabled_plays: string[];
  disableable_plays: string[];
}

/** Read the current global preferences plus the disableable-play menu. */
export async function getPreferences(): Promise<PreferencesData> {
  return callJsonRpc<PreferencesData>("preferences.get");
}

/**
 * Persist the disabled-play set. The sidecar validates each entry against the
 * allowlist (rejecting anything critical) and, if a session is live, reloads
 * its config so the change takes effect mid-run. Returns the new view.
 */
export async function setPreferences(disabledPlays: string[]): Promise<PreferencesData> {
  return callJsonRpc<PreferencesData>("preferences.set", { disabled_plays: disabledPlays });
}
