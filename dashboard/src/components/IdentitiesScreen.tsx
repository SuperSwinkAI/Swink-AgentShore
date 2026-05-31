import React, { useCallback, useEffect, useReducer } from "react";

export interface IdentityRow {
  login: string;
  /** One of: gh_token_login | gh_token_env | gh_token_keychain | ambient */
  source: string;
  /** One of: configured | missing | unknown | ambient */
  token_status: string;
  /** One of: ok | blocked | unknown */
  repo_access: string;
}

/** Result of the identities.check_keychain RPC. */
export interface KeychainStatus {
  login: string;
  service: string;
  has_token: boolean;
}

/** Subset of the sidecar identities.* RPC surface needed by this screen. */
export interface IdentitiesSidecar {
  list(): Promise<IdentityRow[]>;
  add(login: string, tokenSource: string, pat?: string): Promise<void>;
  update(login: string, patch: { token_source: string }): Promise<void>;
  remove(login: string): Promise<void>;
  /**
   * Report whether an AgentShore-managed Keychain PAT already exists for a
   * login. Optional so older/mock sidecars degrade to always requiring a PAT.
   */
  checkKeychain?(login: string): Promise<KeychainStatus>;
}

// ---- state machine --------------------------------------------------------

interface ScreenState {
  rows: IdentityRow[];
  loading: boolean;
  error: string | null;
  editing: string | null;
  editTokenSource: string;
  adding: boolean;
  addLogin: string;
  addLoginError: string | null;
  addTokenSource: string;
  addPat: string;
  /** null = not yet checked; true/false = keychain PAT present for addLogin. */
  addKeychainHasToken: boolean | null;
  addKeychainChecking: boolean;
  busy: Record<string, boolean>;
}

type ScreenAction =
  | { type: "loaded"; rows: IdentityRow[] }
  | { type: "load_error"; message: string }
  | { type: "show_add" }
  | { type: "cancel_add" }
  | { type: "set_add_login"; value: string }
  | { type: "set_add_token_source"; value: string }
  | { type: "set_add_pat"; value: string }
  | { type: "keychain_check_start" }
  | { type: "keychain_checked"; hasToken: boolean }
  | { type: "add_login_error"; message: string }
  | { type: "submit_add" }
  | { type: "add_done"; rows: IdentityRow[] }
  | { type: "add_error"; message: string }
  | { type: "start_edit"; login: string; currentSource: string }
  | { type: "set_edit_token_source"; value: string }
  | { type: "cancel_edit" }
  | { type: "submit_edit" }
  | { type: "edit_done"; rows: IdentityRow[] }
  | { type: "edit_error"; message: string }
  | { type: "submit_remove"; login: string }
  | { type: "remove_done"; login: string; rows: IdentityRow[] }
  | { type: "remove_error"; login: string; message: string };

const INITIAL: ScreenState = {
  rows: [],
  loading: true,
  error: null,
  editing: null,
  editTokenSource: "gh_token_login",
  adding: false,
  addLogin: "",
  addLoginError: null,
  addTokenSource: "gh_token_login",
  addPat: "",
  addKeychainHasToken: null,
  addKeychainChecking: false,
  busy: {},
};

function reducer(state: ScreenState, action: ScreenAction): ScreenState {
  switch (action.type) {
    case "loaded":
      return { ...state, loading: false, error: null, rows: action.rows };
    case "load_error":
      return { ...state, loading: false, error: action.message };
    case "show_add":
      return {
        ...state,
        adding: true,
        addLogin: "",
        addLoginError: null,
        addTokenSource: "gh_token_login",
        addPat: "",
        addKeychainHasToken: null,
        addKeychainChecking: false,
        error: null,
      };
    case "cancel_add":
      return {
        ...state,
        adding: false,
        addLogin: "",
        addLoginError: null,
        addPat: "",
        addKeychainHasToken: null,
        addKeychainChecking: false,
      };
    case "set_add_login":
      // Editing the login invalidates any prior keychain probe result.
      return {
        ...state,
        addLogin: action.value,
        addLoginError: null,
        addKeychainHasToken: null,
      };
    case "set_add_token_source":
      return {
        ...state,
        addTokenSource: action.value,
        addPat: "",
        addKeychainHasToken: null,
        addKeychainChecking: false,
      };
    case "set_add_pat":
      return { ...state, addPat: action.value };
    case "keychain_check_start":
      return { ...state, addKeychainChecking: true };
    case "keychain_checked":
      return {
        ...state,
        addKeychainChecking: false,
        addKeychainHasToken: action.hasToken,
      };
    case "add_login_error":
      return { ...state, addLoginError: action.message };
    case "submit_add":
      return {
        ...state,
        busy: { ...state.busy, __add__: true },
        error: null,
        addLoginError: null,
      };
    case "add_done":
      return {
        ...state,
        busy: { ...state.busy, __add__: false },
        adding: false,
        addLogin: "",
        addPat: "",
        addKeychainHasToken: null,
        addKeychainChecking: false,
        rows: action.rows,
      };
    case "add_error":
      return {
        ...state,
        busy: { ...state.busy, __add__: false },
        error: action.message,
      };
    case "start_edit":
      return {
        ...state,
        editing: action.login,
        editTokenSource: action.currentSource,
        error: null,
      };
    case "set_edit_token_source":
      return { ...state, editTokenSource: action.value };
    case "cancel_edit":
      return { ...state, editing: null };
    case "submit_edit": {
      const login = state.editing ?? "";
      return { ...state, busy: { ...state.busy, [login]: true }, error: null };
    }
    case "edit_done": {
      const login = state.editing ?? "";
      const next = { ...state.busy };
      delete next[login];
      return { ...state, busy: next, editing: null, rows: action.rows };
    }
    case "edit_error": {
      const login = state.editing ?? "";
      const next = { ...state.busy };
      delete next[login];
      return { ...state, busy: next, error: action.message };
    }
    case "submit_remove":
      return {
        ...state,
        busy: { ...state.busy, [action.login]: true },
        error: null,
      };
    case "remove_done": {
      const next = { ...state.busy };
      delete next[action.login];
      return { ...state, busy: next, rows: action.rows };
    }
    case "remove_error": {
      const next = { ...state.busy };
      delete next[action.login];
      return { ...state, busy: next, error: action.message };
    }
  }
}

// ---- badge helpers --------------------------------------------------------

const TOKEN_STATUS_LABELS: Record<string, string> = {
  configured: "Token OK",
  missing: "Token missing",
  unknown: "Token unknown",
  ambient: "Ambient",
};

const TOKEN_STATUS_CLASSES: Record<string, string> = {
  configured: "badge-ok",
  missing: "badge-error",
  unknown: "badge-warn",
  ambient: "badge-warn",
};

const REPO_ACCESS_LABELS: Record<string, string> = {
  ok: "Repo ✓",
  blocked: "Repo ✗",
  unknown: "Repo ?",
};

const REPO_ACCESS_CLASSES: Record<string, string> = {
  ok: "badge-ok",
  blocked: "badge-error",
  unknown: "badge-warn",
};

const SOURCE_LABELS: Record<string, string> = {
  gh_token_login: "gh auth login",
  gh_token_env: "env var",
  gh_token_keychain: "PAT / Keychain",
  ambient: "ambient",
};

function TokenBadge({ status }: { status: string }): React.ReactElement {
  const label = TOKEN_STATUS_LABELS[status] ?? status;
  const cls = TOKEN_STATUS_CLASSES[status] ?? "badge-warn";
  return (
    <span className={`id-badge ${cls}`} data-testid={`token-status-${status}`}>
      {label}
    </span>
  );
}

function RepoBadge({ access }: { access: string }): React.ReactElement {
  const label = REPO_ACCESS_LABELS[access] ?? access;
  const cls = REPO_ACCESS_CLASSES[access] ?? "badge-warn";
  return (
    <span className={`id-badge ${cls}`} data-testid={`repo-access-${access}`}>
      {label}
    </span>
  );
}

// ---- token source selector ------------------------------------------------

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

// ---- identity row ---------------------------------------------------------

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
        <TokenBadge status={row.token_status} />
        <RepoBadge access={row.repo_access} />
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

// ---- main component -------------------------------------------------------

export interface IdentitiesScreenProps {
  sidecar: IdentitiesSidecar;
  onRowsChange?: (rows: IdentityRow[]) => void;
}

export function IdentitiesScreen({
  sidecar,
  onRowsChange,
}: IdentitiesScreenProps): React.ReactElement {
  const [state, dispatch] = useReducer(reducer, INITIAL);

  const reload = useCallback(async () => {
    try {
      const rows = await sidecar.list();
      dispatch({ type: "loaded", rows });
      onRowsChange?.(rows);
    } catch (err) {
      dispatch({ type: "load_error", message: String(err) });
    }
  }, [onRowsChange, sidecar]);

  useEffect(() => {
    void reload();
  }, [reload]);

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
        dispatch({ type: "keychain_checked", hasToken: Boolean(status?.has_token) });
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
      dispatch({ type: "add_login_error", message: "GitHub login is required" });
      return;
    }
    // A PAT is required for Keychain storage unless one is already stored there
    // (in which case a blank field reuses the existing token).
    const keychainPatNeeded =
      state.addTokenSource === "gh_token_keychain" &&
      state.addKeychainHasToken !== true &&
      !state.addPat.trim();
    if (keychainPatNeeded) {
      dispatch({ type: "add_login_error", message: "PAT is required for Keychain storage" });
      return;
    }
    dispatch({ type: "submit_add" });
    try {
      const pat =
        state.addTokenSource === "gh_token_keychain" ? state.addPat.trim() || undefined : undefined;
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
        Configure the GitHub identities AgentShore agents use for commits and pull
        requests. Multiple runners may share the same login — the Code Review
        anti-bias check (reviewer ≠ PR author) is enforced at runtime.
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
    </div>
  );
}
