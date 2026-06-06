import React, { useCallback, useEffect, useReducer } from "react";

/**
 * Trusted sources — no-auth GitHub logins trusted as a *source* of issues and
 * PRs (``trusted_ids.github_logins``). Unlike the agent identities managed by
 * {@link IdentitiesScreen}, these carry no token and are never assigned to an
 * agent: they exist only to mark issue/PR authors AgentShore is allowed to act
 * on when "Only work issues opened by trusted identities" is enabled.
 */

/** Subset of the sidecar identities.* RPC surface this panel needs. */
export interface TrustedSourcesSidecar {
  list(): Promise<string[]>;
  add(login: string): Promise<void>;
  remove(login: string): Promise<void>;
}

interface ScreenState {
  logins: string[];
  loading: boolean;
  error: string | null;
  adding: boolean;
  addLogin: string;
  addError: string | null;
  busy: Record<string, boolean>;
}

type ScreenAction =
  | { type: "loaded"; logins: string[] }
  | { type: "load_error"; message: string }
  | { type: "show_add" }
  | { type: "cancel_add" }
  | { type: "set_add_login"; value: string }
  | { type: "add_error"; message: string }
  | { type: "submit_add" }
  | { type: "add_done"; logins: string[] }
  | { type: "submit_remove"; login: string }
  | { type: "remove_done"; login: string; logins: string[] }
  | { type: "remove_error"; login: string; message: string };

function initialState(seed: readonly string[] | undefined): ScreenState {
  const logins = seed && seed.length > 0 ? [...seed] : [];
  return {
    // Pre-paint the hydrated seed (if any) so the rail shows the previous
    // session's trusted sources immediately. ``loading`` stays true so the
    // sidecar ``list()`` still runs and reconciles authoritatively — the
    // seed is parity chrome, not a replacement for the self-load.
    logins,
    loading: true,
    error: null,
    adding: false,
    addLogin: "",
    addError: null,
    busy: {},
  };
}

function reducer(state: ScreenState, action: ScreenAction): ScreenState {
  switch (action.type) {
    case "loaded":
      return { ...state, loading: false, error: null, logins: action.logins };
    case "load_error":
      return { ...state, loading: false, error: action.message };
    case "show_add":
      return {
        ...state,
        adding: true,
        addLogin: "",
        addError: null,
        error: null,
      };
    case "cancel_add":
      return { ...state, adding: false, addLogin: "", addError: null };
    case "set_add_login":
      return { ...state, addLogin: action.value, addError: null };
    case "add_error":
      return {
        ...state,
        busy: { ...state.busy, __add__: false },
        addError: action.message,
      };
    case "submit_add":
      return {
        ...state,
        busy: { ...state.busy, __add__: true },
        error: null,
        addError: null,
      };
    case "add_done":
      return {
        ...state,
        busy: { ...state.busy, __add__: false },
        adding: false,
        addLogin: "",
        logins: action.logins,
      };
    case "submit_remove":
      return {
        ...state,
        busy: { ...state.busy, [action.login]: true },
        error: null,
      };
    case "remove_done": {
      const next = { ...state.busy };
      delete next[action.login];
      return { ...state, busy: next, logins: action.logins };
    }
    case "remove_error": {
      const next = { ...state.busy };
      delete next[action.login];
      return { ...state, busy: next, error: action.message };
    }
  }
}

export interface TrustedSourcesScreenProps {
  sidecar: TrustedSourcesSidecar;
  onSourcesChange?: (logins: string[]) => void;
  /** Optional hydrated trusted-source logins to pre-paint before the
   *  sidecar ``list()`` resolves. The panel always self-loads from the
   *  sidecar and reconciles to its result; this seed only avoids a blank
   *  flash on re-entry (parity with localStorage-hydrated panels). */
  initialLogins?: readonly string[];
}

export function TrustedSourcesScreen({
  sidecar,
  onSourcesChange,
  initialLogins,
}: TrustedSourcesScreenProps): React.ReactElement {
  const [state, dispatch] = useReducer(reducer, initialLogins, initialState);

  const reload = useCallback(async () => {
    try {
      const logins = await sidecar.list();
      dispatch({ type: "loaded", logins });
      onSourcesChange?.(logins);
    } catch (err) {
      dispatch({ type: "load_error", message: String(err) });
    }
  }, [onSourcesChange, sidecar]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function handleAdd(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    const login = state.addLogin.trim();
    if (!login) {
      dispatch({ type: "add_error", message: "GitHub login is required" });
      return;
    }
    dispatch({ type: "submit_add" });
    try {
      await sidecar.add(login);
      const logins = await sidecar.list();
      dispatch({ type: "add_done", logins });
      onSourcesChange?.(logins);
    } catch (err) {
      dispatch({ type: "add_error", message: String(err) });
    }
  }

  async function handleRemove(login: string): Promise<void> {
    dispatch({ type: "submit_remove", login });
    try {
      await sidecar.remove(login);
      const logins = await sidecar.list();
      dispatch({ type: "remove_done", login, logins });
      onSourcesChange?.(logins);
    } catch (err) {
      dispatch({ type: "remove_error", login, message: String(err) });
    }
  }

  return (
    <div
      className="id-screen id-trusted-screen"
      data-testid="trusted-sources-screen"
    >
      <h3 className="id-subtitle">Trusted sources</h3>
      <p className="id-description">
        GitHub logins trusted as a source of issues and pull requests, but never
        assigned to an agent — no token required. AgentShore reads their
        issues/PRs when "Only work issues opened by trusted identities" is on.
      </p>

      {state.error && (
        <div
          className="id-error"
          role="alert"
          data-testid="trusted-sources-error"
        >
          {state.error}
        </div>
      )}

      {state.loading && state.logins.length === 0 ? (
        <p className="id-loading" data-testid="trusted-sources-loading">
          Loading trusted sources…
        </p>
      ) : state.logins.length === 0 ? (
        <p className="id-empty" data-testid="trusted-sources-empty">
          No trusted sources. Add a GitHub login below.
        </p>
      ) : (
        <ul className="id-list" data-testid="trusted-sources-list">
          {state.logins.map((login) => (
            <li
              key={login}
              className="id-row id-trusted-row"
              data-login={login}
              data-testid={`trusted-source-row-${login}`}
            >
              <div className="id-row-main">
                <span className="id-login">{login}</span>
                <span className="id-source-label">no auth · source only</span>
              </div>
              <div className="id-row-actions">
                <button
                  type="button"
                  className="id-btn id-btn-danger"
                  onClick={() => void handleRemove(login)}
                  disabled={Boolean(state.busy[login])}
                  data-testid={`trusted-remove-btn-${login}`}
                >
                  {state.busy[login] ? "Removing…" : "Remove"}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {state.adding ? (
        <form
          className="id-add-form"
          onSubmit={(e) => void handleAdd(e)}
          data-testid="add-trusted-source-form"
        >
          {state.addError && (
            <div
              className="id-field-error"
              role="alert"
              data-testid="add-trusted-error"
            >
              {state.addError}
            </div>
          )}
          <div className="id-form-row">
            <label htmlFor="add-trusted-login" className="id-label">
              GitHub login
            </label>
            <input
              id="add-trusted-login"
              type="text"
              className="id-input"
              value={state.addLogin}
              onChange={(e) =>
                dispatch({ type: "set_add_login", value: e.target.value })
              }
              placeholder="dependabot[bot]"
              autoFocus
              data-testid="add-trusted-login-input"
            />
            <span className="id-hint">(no auth)</span>
          </div>
          <div className="id-form-actions">
            <button
              type="submit"
              className="id-btn id-btn-primary"
              disabled={Boolean(state.busy["__add__"])}
              data-testid="add-trusted-submit-btn"
            >
              {state.busy["__add__"] ? "Adding…" : "Add trusted source"}
            </button>
            <button
              type="button"
              className="id-btn"
              onClick={() => dispatch({ type: "cancel_add" })}
              disabled={Boolean(state.busy["__add__"])}
              data-testid="add-trusted-cancel-btn"
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
          data-testid="show-add-trusted-btn"
        >
          + Add trusted source
        </button>
      )}
    </div>
  );
}
