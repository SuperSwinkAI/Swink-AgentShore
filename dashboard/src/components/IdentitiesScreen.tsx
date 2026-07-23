import React, { useCallback, useEffect } from "react";

import { useIdentitiesReducer } from "./useIdentitiesReducer";

export interface IdentityRow {
  login: string;
  /** One of: gh_token_login | gh_token_env | gh_token_keychain | ambient */
  source: string;
  /** Source-specific credential state. */
  token_status: string;
  /** One of: ok | blocked | unknown | checking | check_failed */
  repo_access: string;
  repo_access_detail?: string;
}

/** Result of the identities.check_keychain RPC. */
export interface KeychainStatus {
  login: string;
  service: string;
  has_token: boolean;
}

/**
 * One row from the agents.check_auth RPC: a single CLI agent's backend
 * (model-provider) auth state.
 *
 * `status` is one of: ok | unprobeable | expired | timeout | error.
 * `ok`/`unprobeable` are healthy; `expired` is the blocking failure;
 * `timeout`/`error` are surfaced but non-blocking. `detail` carries a
 * human-readable hint (e.g. a remediation command like `run 'codex login'`).
 */
export interface AgentAuthRow {
  agent_type: string;
  status: string;
  detail: string;
}

/** Subset of the sidecar identities.* RPC surface needed by this screen. */
export interface IdentitiesSidecar {
  list(): Promise<IdentityRow[]>;
  add(login: string, tokenSource: string, pat?: string): Promise<void>;
  update(login: string, patch: { token_source: string }): Promise<void>;
  remove(login: string): Promise<void>;
  /**
   * Resolve one identity's token and verify whether it can access the current
   * repository. Optional so older sidecars still render the configured list.
   */
  checkAccess?(login: string): Promise<IdentityRow>;
  /**
   * Report whether an AgentShore-managed Keychain PAT already exists for a
   * login. Optional so older/mock sidecars degrade to always requiring a PAT.
   */
  checkKeychain?(login: string): Promise<KeychainStatus>;
  /** Probe each configured CLI agent's backend (model-provider) auth. */
  checkAgentAuth?(): Promise<AgentAuthRow[]>;
}

// badge helpers

const TOKEN_STATUS_LABELS: Record<string, string> = {
  configured: "Token OK",
  missing: "Token missing",
  unknown: "Token unknown",
  ambient: "Ambient",
  auth_ok: "GH auth OK",
  auth_missing: "GH auth missing",
  auth_timeout: "GH auth timeout",
  auth_error: "GH auth error",
  auth_mismatch: "GH auth mismatch",
  token_timeout: "Token timeout",
};

const TOKEN_STATUS_CLASSES: Record<string, string> = {
  configured: "badge-ok",
  missing: "badge-error",
  unknown: "badge-warn",
  ambient: "badge-warn",
  auth_ok: "badge-ok",
  auth_missing: "badge-error",
  auth_timeout: "badge-error",
  auth_error: "badge-error",
  auth_mismatch: "badge-error",
  token_timeout: "badge-error",
};

const REPO_ACCESS_LABELS: Record<string, string> = {
  ok: "Repo ✓",
  blocked: "Repo ✗",
  unknown: "Repo ?",
  checking: "Repo checking...",
  check_failed: "Repo check failed",
};

const REPO_ACCESS_CLASSES: Record<string, string> = {
  ok: "badge-ok",
  blocked: "badge-error",
  unknown: "badge-warn",
  checking: "badge-warn",
  check_failed: "badge-error",
};

const SOURCE_LABELS: Record<string, string> = {
  gh_token_login: "gh auth login",
  gh_token_env: "env var",
  gh_token_keychain: "PAT / Keychain",
  ambient: "ambient",
};

const SKIP_ACCESS_STATUSES = new Set([
  "missing",
  "auth_missing",
  "auth_timeout",
  "auth_error",
  "auth_mismatch",
  "token_timeout",
  "ambient",
]);

function shouldSkipAccessCheck(row: IdentityRow): boolean {
  return row.source === "ambient" || SKIP_ACCESS_STATUSES.has(row.token_status);
}

function credentialLabel(source: string, status: string): string {
  if (source === "gh_token_login") {
    if (status === "configured") return "GH auth set";
    if (status === "missing") return "GH auth missing";
  }
  return TOKEN_STATUS_LABELS[status] ?? status;
}

function credentialClass(source: string, status: string): string {
  if (source === "gh_token_login" && status === "configured") {
    return "badge-warn";
  }
  return TOKEN_STATUS_CLASSES[status] ?? "badge-warn";
}

function TokenBadge({
  source,
  status,
}: {
  source: string;
  status: string;
}): React.ReactElement {
  const label = credentialLabel(source, status);
  const cls = credentialClass(source, status);
  return (
    <span className={`id-badge ${cls}`} data-testid={`token-status-${status}`}>
      {label}
    </span>
  );
}

function RepoBadge({
  access,
  detail,
}: {
  access: string;
  detail?: string;
}): React.ReactElement {
  const label = REPO_ACCESS_LABELS[access] ?? access;
  const cls = REPO_ACCESS_CLASSES[access] ?? "badge-warn";
  return (
    <span
      className={`id-badge ${cls}`}
      data-testid={`repo-access-${access}`}
      title={detail}
    >
      {label}
    </span>
  );
}

// agent backend auth badges

const AGENT_AUTH_LABELS: Record<string, string> = {
  ok: "Backend auth OK",
  unprobeable: "Not probeable",
  expired: "Backend auth expired",
  timeout: "Auth check timed out",
  error: "Auth check error",
};

const AGENT_AUTH_CLASSES: Record<string, string> = {
  ok: "badge-ok",
  unprobeable: "badge-info",
  expired: "badge-error",
  timeout: "badge-warn",
  error: "badge-error",
};

/** Statuses that warrant surfacing the detail string as a remediation hint. */
const AGENT_AUTH_DETAIL_STATUSES = new Set(["expired", "timeout", "error"]);

function AgentAuthBadge({ status }: { status: string }): React.ReactElement {
  const label = AGENT_AUTH_LABELS[status] ?? status;
  const cls = AGENT_AUTH_CLASSES[status] ?? "badge-warn";
  return (
    <span
      className={`id-badge ${cls}`}
      data-testid={`agent-auth-status-${status}`}
    >
      {label}
    </span>
  );
}

// token source selector

const TOKEN_SOURCE_OPTIONS = [
  { value: "gh_token_login", label: "gh auth login" },
  { value: "gh_token_env", label: "env var" },
  { value: "gh_token_keychain", label: "PAT (stored in Keychain)" },
] as const;

function TokenSourceSelect({
  value,
  onChange,
  id,
}: {
  value: string;
  onChange: (v: string) => void;
  id: string;
}): React.ReactElement {
  return (
    <select
      id={id}
      className="id-select"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      {TOKEN_SOURCE_OPTIONS.map((opt) => (
        <option key={opt.value} value={opt.value}>
          {opt.label}
        </option>
      ))}
    </select>
  );
}

// identity row

interface IdentityRowItemProps {
  row: IdentityRow;
  isEditing: boolean;
  editTokenSource: string;
  busy: boolean;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onChangeEditTokenSource: (v: string) => void;
  onSaveEdit: () => void;
  onRemove: () => void;
}

function IdentityRowItem({
  row,
  isEditing,
  editTokenSource,
  busy,
  onStartEdit,
  onCancelEdit,
  onChangeEditTokenSource,
  onSaveEdit,
  onRemove,
}: IdentityRowItemProps): React.ReactElement {
  return (
    <li
      className="id-row"
      data-login={row.login}
      data-testid={`identity-row-${row.login}`}
    >
      <div className="id-row-main">
        <span className="id-login">{row.login}</span>
        <span className="id-source-label">
          {SOURCE_LABELS[row.source] ?? row.source}
        </span>
        <TokenBadge source={row.source} status={row.token_status} />
        <RepoBadge access={row.repo_access} detail={row.repo_access_detail} />
        {row.repo_access_detail && row.repo_access !== "ok" && (
          <span
            className="id-access-detail"
            data-testid={`repo-access-detail-${row.login}`}
          >
            {row.repo_access_detail}
          </span>
        )}
      </div>

      {isEditing ? (
        <div className="id-edit-row" data-testid={`edit-form-${row.login}`}>
          <label htmlFor={`edit-source-${row.login}`} className="id-label">
            Token source
          </label>
          <TokenSourceSelect
            id={`edit-source-${row.login}`}
            value={editTokenSource}
            onChange={onChangeEditTokenSource}
          />
          <button
            type="button"
            className="id-btn id-btn-primary"
            onClick={onSaveEdit}
            disabled={busy}
            data-testid={`save-edit-${row.login}`}
          >
            {busy ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            className="id-btn"
            onClick={onCancelEdit}
            disabled={busy}
            data-testid={`cancel-edit-${row.login}`}
          >
            Cancel
          </button>
        </div>
      ) : (
        <div className="id-row-actions">
          <button
            type="button"
            className="id-btn"
            onClick={onStartEdit}
            disabled={busy}
            data-testid={`edit-btn-${row.login}`}
          >
            Edit
          </button>
          <button
            type="button"
            className="id-btn id-btn-danger"
            onClick={onRemove}
            disabled={busy}
            data-testid={`remove-btn-${row.login}`}
          >
            {busy ? "Removing…" : "Remove"}
          </button>
        </div>
      )}
    </li>
  );
}

// main component

export interface IdentitiesScreenProps {
  sidecar: IdentitiesSidecar;
  onRowsChange?: (rows: IdentityRow[]) => void;
}

export function IdentitiesScreen({
  sidecar,
  onRowsChange,
}: IdentitiesScreenProps): React.ReactElement {
  const [state, dispatch] = useIdentitiesReducer();

  const checkAccessForRows = useCallback(
    (rows: IdentityRow[]) => {
      const checkAccess = sidecar.checkAccess;
      if (!checkAccess) return;
      void (async () => {
        for (const row of rows) {
          if (shouldSkipAccessCheck(row)) continue;
          dispatch({
            type: "access_check_started",
            login: row.login,
            source: row.source,
          });
          try {
            const checked = await checkAccess(row.login);
            dispatch({ type: "access_checked", row: checked });
          } catch (err) {
            dispatch({
              type: "access_checked",
              row: {
                ...row,
                repo_access: "check_failed",
                repo_access_detail:
                  row.source === "gh_token_login"
                    ? `Unable to run GitHub CLI auth verification: ${String(err)}`
                    : `Unable to run GitHub token repository access verification: ${String(err)}`,
              },
            });
          }
        }
      })();
    },
    [sidecar],
  );

  const reload = useCallback(async () => {
    try {
      const rows = await sidecar.list();
      dispatch({ type: "loaded", rows });
      onRowsChange?.(rows);
      checkAccessForRows(rows);
    } catch (err) {
      dispatch({ type: "load_error", message: String(err) });
    }
  }, [checkAccessForRows, onRowsChange, sidecar]);

  useEffect(() => {
    void reload();
  }, [reload]);

  // Probe each configured CLI agent's backend auth. Optional RPC: when the
  // sidecar predates `agents.check_auth`, the section quietly stays hidden.
  const checkAgentAuth = useCallback(async () => {
    const probe = sidecar.checkAgentAuth;
    if (!probe) return;
    dispatch({ type: "agent_auth_started" });
    try {
      const rows = await probe();
      dispatch({ type: "agent_auth_loaded", rows });
    } catch (err) {
      dispatch({ type: "agent_auth_error", message: String(err) });
    }
  }, [sidecar]);

  useEffect(() => {
    void checkAgentAuth();
  }, [checkAgentAuth]);

  // Probe the Keychain for an already-stored PAT so we can offer to reuse it
  // instead of forcing a re-paste (mirrors the CLI wizard's pre-flight check).
  const probeKeychain = useCallback(
    async (login: string): Promise<void> => {
      if (!sidecar.checkKeychain) return;
      const trimmed = login.trim();
      if (!trimmed) {
        dispatch({ type: "keychain_checked", hasToken: false });
        return;
      }
      dispatch({ type: "keychain_check_start" });
      try {
        const status = await sidecar.checkKeychain(trimmed);
        dispatch({
          type: "keychain_checked",
          hasToken: Boolean(status?.has_token),
        });
      } catch {
        // A failed probe just falls back to requiring a PAT — never blocks add.
        dispatch({ type: "keychain_checked", hasToken: false });
      }
    },
    [sidecar],
  );

  async function handleAdd(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    const login = state.addLogin.trim();
    if (!login) {
      dispatch({
        type: "add_login_error",
        message: "GitHub login is required",
      });
      return;
    }
    // A PAT is required for Keychain storage unless one is already stored there
    // (in which case a blank field reuses the existing token).
    const keychainPatNeeded =
      state.addTokenSource === "gh_token_keychain" &&
      state.addKeychainHasToken !== true &&
      !state.addPat.trim();
    if (keychainPatNeeded) {
      dispatch({
        type: "add_login_error",
        message: "PAT is required for Keychain storage",
      });
      return;
    }
    dispatch({ type: "submit_add" });
    try {
      const pat =
        state.addTokenSource === "gh_token_keychain"
          ? state.addPat.trim() || undefined
          : undefined;
      await sidecar.add(login, state.addTokenSource, pat);
      const rows = await sidecar.list();
      dispatch({ type: "add_done", rows });
      onRowsChange?.(rows);
    } catch (err) {
      dispatch({ type: "add_error", message: String(err) });
    }
  }

  async function handleSaveEdit(login: string): Promise<void> {
    dispatch({ type: "submit_edit" });
    try {
      await sidecar.update(login, { token_source: state.editTokenSource });
      const rows = await sidecar.list();
      dispatch({ type: "edit_done", rows });
      onRowsChange?.(rows);
    } catch (err) {
      dispatch({ type: "edit_error", message: String(err) });
    }
  }

  async function handleRemove(login: string): Promise<void> {
    dispatch({ type: "submit_remove", login });
    try {
      await sidecar.remove(login);
      const rows = await sidecar.list();
      dispatch({ type: "remove_done", login, rows });
      onRowsChange?.(rows);
    } catch (err) {
      dispatch({ type: "remove_error", login, message: String(err) });
    }
  }

  if (state.loading) {
    return (
      <div className="id-screen" data-testid="identities-screen">
        <p className="id-loading" data-testid="identities-loading">
          Loading identities…
        </p>
      </div>
    );
  }

  return (
    <div className="id-screen" data-testid="identities-screen">
      <h2 className="id-title">Trusted identities</h2>
      <p className="id-description">
        Configure the GitHub identities AgentShore agents use for commits and
        pull requests. Multiple runners may share the same login — the Code
        Review anti-bias check (reviewer ≠ PR author) is enforced at runtime.
      </p>

      {state.error && (
        <div className="id-error" role="alert" data-testid="identities-error">
          {state.error}
        </div>
      )}

      {state.rows.length === 0 ? (
        <p className="id-empty" data-testid="identities-empty">
          No identities configured. Add one below.
        </p>
      ) : (
        <ul className="id-list" data-testid="identities-list">
          {state.rows.map((row) => (
            <IdentityRowItem
              key={row.login}
              row={row}
              isEditing={state.editing === row.login}
              editTokenSource={state.editTokenSource}
              busy={Boolean(state.busy[row.login])}
              onStartEdit={() =>
                dispatch({
                  type: "start_edit",
                  login: row.login,
                  currentSource: row.source,
                })
              }
              onCancelEdit={() => dispatch({ type: "cancel_edit" })}
              onChangeEditTokenSource={(v) =>
                dispatch({ type: "set_edit_token_source", value: v })
              }
              onSaveEdit={() => void handleSaveEdit(row.login)}
              onRemove={() => void handleRemove(row.login)}
            />
          ))}
        </ul>
      )}

      {state.adding ? (
        <form
          className="id-add-form"
          onSubmit={(e) => void handleAdd(e)}
          data-testid="add-identity-form"
        >
          <h3 className="id-add-title">Add identity</h3>
          {state.addLoginError && (
            <div
              className="id-field-error"
              role="alert"
              data-testid="add-login-error"
            >
              {state.addLoginError}
            </div>
          )}
          <div className="id-form-row">
            <label htmlFor="add-login" className="id-label">
              GitHub login
            </label>
            <input
              id="add-login"
              type="text"
              className="id-input"
              value={state.addLogin}
              onChange={(e) =>
                dispatch({ type: "set_add_login", value: e.target.value })
              }
              onBlur={(e) => {
                if (state.addTokenSource === "gh_token_keychain") {
                  void probeKeychain(e.target.value);
                }
              }}
              placeholder="octocat"
              autoFocus
              data-testid="add-login-input"
            />
          </div>
          <div className="id-form-row">
            <label htmlFor="add-token-source" className="id-label">
              Token source
            </label>
            <TokenSourceSelect
              id="add-token-source"
              value={state.addTokenSource}
              onChange={(v) => {
                dispatch({ type: "set_add_token_source", value: v });
                if (v === "gh_token_keychain" && state.addLogin.trim()) {
                  void probeKeychain(state.addLogin);
                }
              }}
            />
          </div>
          {state.addTokenSource === "gh_token_keychain" && (
            <div className="id-form-row">
              <label htmlFor="add-pat" className="id-label">
                Personal Access Token
              </label>
              {state.addKeychainHasToken === true && (
                <p className="id-hint" data-testid="keychain-existing-pat">
                  A PAT for this login is already stored in your Keychain. Leave
                  this blank to reuse it, or enter a new one to replace it.
                </p>
              )}
              <input
                id="add-pat"
                type="password"
                className="id-input"
                value={state.addPat}
                onChange={(e) =>
                  dispatch({ type: "set_add_pat", value: e.target.value })
                }
                placeholder={
                  state.addKeychainHasToken === true
                    ? "Using stored PAT — enter a new one to replace"
                    : "ghp_…"
                }
                autoComplete="off"
                data-testid="add-pat-input"
              />
            </div>
          )}
          <div className="id-form-actions">
            <button
              type="submit"
              className="id-btn id-btn-primary"
              disabled={Boolean(state.busy["__add__"])}
              data-testid="add-submit-btn"
            >
              {state.busy["__add__"] ? "Adding…" : "Add identity"}
            </button>
            <button
              type="button"
              className="id-btn"
              onClick={() => dispatch({ type: "cancel_add" })}
              disabled={Boolean(state.busy["__add__"])}
              data-testid="add-cancel-btn"
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button
          type="button"
          className="id-btn id-add-trigger"
          onClick={() => dispatch({ type: "show_add" })}
          data-testid="show-add-form-btn"
        >
          + Add identity
        </button>
      )}

      {sidecar.checkAgentAuth && (
        <section
          className="id-agent-auth"
          aria-labelledby="agent-auth-heading"
          data-testid="agent-auth-section"
        >
          <div className="id-agent-auth-header">
            <h3 className="id-agent-auth-title" id="agent-auth-heading">
              Agent backend auth
            </h3>
            <button
              type="button"
              className="id-btn"
              onClick={() => void checkAgentAuth()}
              disabled={state.agentAuthLoading}
              data-testid="agent-auth-verify-btn"
            >
              {state.agentAuthLoading ? "Checking…" : "Verify"}
            </button>
          </div>
          <p className="id-description">
            Each CLI agent authenticates to its own model provider (e.g. Codex
            uses a chatgpt.com session). These tokens can expire mid-run — verify
            them here before starting a session.
          </p>

          {state.agentAuthError && (
            <div
              className="id-error"
              role="alert"
              data-testid="agent-auth-error"
            >
              {state.agentAuthError}
            </div>
          )}

          {state.agentAuthLoading && state.agentAuth === null ? (
            <p
              className="id-loading"
              data-testid="agent-auth-loading"
              role="status"
            >
              Checking agent backend auth…
            </p>
          ) : state.agentAuth && state.agentAuth.length > 0 ? (
            <ul className="id-list" data-testid="agent-auth-list">
              {state.agentAuth.map((row) => (
                <li
                  key={row.agent_type}
                  className="id-row"
                  data-agent-type={row.agent_type}
                  data-testid={`agent-auth-row-${row.agent_type}`}
                >
                  <div className="id-row-main">
                    <span className="id-login">{row.agent_type}</span>
                    <AgentAuthBadge status={row.status} />
                    {row.detail && AGENT_AUTH_DETAIL_STATUSES.has(row.status) && (
                      <span
                        className="id-access-detail"
                        data-testid={`agent-auth-detail-${row.agent_type}`}
                      >
                        {row.detail}
                      </span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            state.agentAuth !== null &&
            !state.agentAuthError && (
              <p className="id-empty" data-testid="agent-auth-empty">
                No CLI agents configured.
              </p>
            )
          )}
        </section>
      )}
    </div>
  );
}
