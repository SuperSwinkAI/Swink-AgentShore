import { callJsonRpc } from "./jsonrpc";

export interface IdentityRow {
  login: string;
  source: string;
  token_status: string;
  repo_access: string;
  repo_access_detail?: string;
}

export async function listIdentities(): Promise<IdentityRow[]> {
  const result = await callJsonRpc<IdentityRow[] | null>("identities.list");
  return result ?? [];
}

export interface KeychainStatus {
  login: string;
  service: string;
  has_token: boolean;
}

export async function checkKeychainToken(
  login: string,
): Promise<KeychainStatus> {
  const result = await callJsonRpc<KeychainStatus | null>(
    "identities.check_keychain",
    { login },
  );
  return result ?? { login, service: "", has_token: false };
}

export async function checkIdentityAccess(login: string): Promise<IdentityRow> {
  const result = await callJsonRpc<IdentityRow | null>(
    "identities.check_access",
    { login },
  );
  return (
    result ?? {
      login,
      source: "ambient",
      token_status: "ambient",
      repo_access: "unknown",
    }
  );
}

export async function addIdentity(
  login: string,
  tokenSource: string,
  pat?: string,
): Promise<void> {
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

// ---- trusted sources (no-auth, issues/PRs only) ---------------------------
// These map to trusted_ids.github_logins: GitHub logins trusted as a source of
// issues/PRs but never assigned to an agent and carrying no token.

export async function listTrustedSources(): Promise<string[]> {
  const result = await callJsonRpc<string[] | null>("identities.list_trusted");
  return result ?? [];
}

export async function addTrustedSource(login: string): Promise<void> {
  await callJsonRpc("identities.add_trusted", { login });
}

export async function removeTrustedSource(login: string): Promise<void> {
  await callJsonRpc("identities.remove_trusted", { login });
}
