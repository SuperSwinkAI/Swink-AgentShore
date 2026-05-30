import { callJsonRpc } from "./jsonrpc";

export interface IdentityRow {
  login: string;
  source: string;
  token_status: string;
  repo_access: string;
}

export async function listIdentities(): Promise<IdentityRow[]> {
  const result = await callJsonRpc<IdentityRow[] | null>("identities.list");
  return result ?? [];
}

export async function addIdentity(login: string, tokenSource: string, pat?: string): Promise<void> {
  const params: Record<string, string> = { login, token_source: tokenSource };
  if (pat) {
    params.pat = pat;
  }
  await callJsonRpc("identities.add", params);
}

export async function updateIdentity(
  login: string,
  patch: { token_source: string },
): Promise<void> {
  await callJsonRpc("identities.update", { login, patch });
}

export async function removeIdentity(login: string): Promise<void> {
  await callJsonRpc("identities.remove", { login });
}
