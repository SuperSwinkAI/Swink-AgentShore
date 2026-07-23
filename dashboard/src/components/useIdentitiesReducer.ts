import { useReducer } from "react";
import type { Dispatch } from "react";

import type { AgentAuthRow, IdentityRow } from "./IdentitiesScreen";

// state machine

export interface ScreenState {
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
  /** null = not yet probed (or sidecar lacks the RPC); [] = probed, none. */
  agentAuth: AgentAuthRow[] | null;
  agentAuthLoading: boolean;
  agentAuthError: string | null;
}

export type ScreenAction =
  | { type: "loaded"; rows: IdentityRow[] }
  | { type: "load_error"; message: string }
  | { type: "access_check_started"; login: string; source: string }
  | { type: "access_checked"; row: IdentityRow }
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
  | { type: "remove_error"; login: string; message: string }
  | { type: "agent_auth_started" }
  | { type: "agent_auth_loaded"; rows: AgentAuthRow[] }
  | { type: "agent_auth_error"; message: string };

export const INITIAL: ScreenState = {
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
  agentAuth: null,
  agentAuthLoading: false,
  agentAuthError: null,
};

export function reducer(state: ScreenState, action: ScreenAction): ScreenState {
  switch (action.type) {
    case "loaded":
      return { ...state, loading: false, error: null, rows: action.rows };
    case "load_error":
      return { ...state, loading: false, error: action.message };
    case "access_check_started": {
      const rows = state.rows.map((row) =>
        row.login === action.login
          ? {
              ...row,
              repo_access: "checking",
              repo_access_detail:
                action.source === "gh_token_login"
                  ? "Verifying GitHub CLI auth and repository access..."
                  : "Verifying GitHub token and repository access...",
            }
          : row,
      );
      return { ...state, rows };
    }
    case "access_checked": {
      const rows = state.rows.map((row) =>
        row.login === action.row.login ? { ...row, ...action.row } : row,
      );
      return { ...state, rows };
    }
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
    case "agent_auth_started":
      return { ...state, agentAuthLoading: true, agentAuthError: null };
    case "agent_auth_loaded":
      return {
        ...state,
        agentAuthLoading: false,
        agentAuthError: null,
        agentAuth: action.rows,
      };
    case "agent_auth_error":
      return {
        ...state,
        agentAuthLoading: false,
        agentAuthError: action.message,
      };
  }
}

export function useIdentitiesReducer(): [ScreenState, Dispatch<ScreenAction>] {
  return useReducer(reducer, INITIAL);
}
