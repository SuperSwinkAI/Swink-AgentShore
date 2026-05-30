import { callJsonRpc } from "./jsonrpc";

export interface RecentEntry {
  path: string;
  label: string;
  last_started: string;
  last_exit_reason: string | null;
  has_valid_config: boolean;
}

export async function listRecents(): Promise<RecentEntry[]> {
  const result = await callJsonRpc<RecentEntry[] | null>("recents.list");
  return result ?? [];
}

export async function touchRecent(path: string): Promise<void> {
  await callJsonRpc<unknown>("recents.touch", { path });
}

export async function removeRecent(path: string): Promise<void> {
  await callJsonRpc<unknown>("recents.remove", { path });
}
