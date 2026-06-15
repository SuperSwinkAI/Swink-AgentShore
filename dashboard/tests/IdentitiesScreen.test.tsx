/**
 * Unit tests for IdentitiesScreen (Screen 4 of the desktop setup wizard).
 *
 * Uses react-dom/client + act directly (no @testing-library/react) to stay
 * consistent with the rest of the dashboard test suite.
 */

import React from "react";
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import {
  IdentitiesScreen,
  type AgentAuthRow,
  type IdentitiesSidecar,
  type IdentityRow,
} from "../src/components/IdentitiesScreen";

// ---- helpers ---------------------------------------------------------------

function makeSidecar(rows: IdentityRow[] = []): IdentitiesSidecar & {
  _rows: IdentityRow[];
  addCalls: Array<{ login: string; tokenSource: string }>;
  updateCalls: Array<{ login: string; patch: { token_source: string } }>;
  removeCalls: string[];
} {
  const sidecar = {
    _rows: [...rows],
    addCalls: [] as Array<{ login: string; tokenSource: string }>,
    updateCalls: [] as Array<{
      login: string;
      patch: { token_source: string };
    }>,
    removeCalls: [] as string[],

    async list() {
      return [...sidecar._rows];
    },
    async add(login: string, tokenSource: string) {
      sidecar.addCalls.push({ login, tokenSource });
      sidecar._rows.push({
        login: login.toLowerCase(),
        source: tokenSource,
        token_status: "configured",
        repo_access: "ok",
      });
    },
    async update(login: string, patch: { token_source: string }) {
      sidecar.updateCalls.push({ login, patch });
      const row = sidecar._rows.find((r) => r.login === login);
      if (row) row.source = patch.token_source;
    },
    async remove(login: string) {
      sidecar.removeCalls.push(login);
      sidecar._rows = sidecar._rows.filter((r) => r.login !== login);
    },
  };
  return sidecar;
}

function getTestId(container: HTMLElement, id: string): HTMLElement | null {
  return container.querySelector(`[data-testid="${id}"]`);
}

function requireTestId(container: HTMLElement, id: string): HTMLElement {
  const el = getTestId(container, id);
  if (!el) throw new Error(`Element with data-testid="${id}" not found`);
  return el;
}

// ---- test setup ------------------------------------------------------------

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
});

async function render(sidecar: IdentitiesSidecar): Promise<void> {
  await act(async () => {
    root.render(<IdentitiesScreen sidecar={sidecar} />);
  });
}

async function renderWithRowsChange(
  sidecar: IdentitiesSidecar,
  onRowsChange: (rows: IdentityRow[]) => void,
): Promise<void> {
  await act(async () => {
    root.render(
      <IdentitiesScreen sidecar={sidecar} onRowsChange={onRowsChange} />,
    );
  });
}

// ---- tests -----------------------------------------------------------------

describe("IdentitiesScreen", () => {
  it("shows loading state initially and resolves to empty message", async () => {
    let resolveList!: (rows: IdentityRow[]) => void;
    const lazySidecar: IdentitiesSidecar = {
      list: () =>
        new Promise((res) => {
          resolveList = res;
        }),
      add: vi.fn(),
      update: vi.fn(),
      remove: vi.fn(),
    };

    // Start render — list() hasn't resolved yet
    await act(async () => {
      root.render(<IdentitiesScreen sidecar={lazySidecar} />);
    });

    expect(getTestId(container, "identities-loading")).not.toBeNull();

    // Resolve the list
    await act(async () => {
      resolveList([]);
    });

    expect(getTestId(container, "identities-loading")).toBeNull();
    expect(getTestId(container, "identities-empty")).not.toBeNull();
  });

  it("reports loaded rows to the setup gate", async () => {
    const rows: IdentityRow[] = [
      {
        login: "octocat",
        source: "gh_token_login",
        token_status: "configured",
        repo_access: "ok",
      },
    ];
    const sidecar = makeSidecar(rows);
    const onRowsChange = vi.fn();

    await renderWithRowsChange(sidecar, onRowsChange);

    expect(onRowsChange).toHaveBeenCalledWith(rows);
  });

  it("renders a row for each identity with correct badges", async () => {
    const sidecar = makeSidecar([
      {
        login: "octocat",
        source: "gh_token_login",
        token_status: "configured",
        repo_access: "ok",
      },
      {
        login: "bot-user",
        source: "gh_token_env",
        token_status: "missing",
        repo_access: "unknown",
      },
    ]);

    await render(sidecar);

    const list = requireTestId(container, "identities-list");
    expect(list.querySelectorAll("li")).toHaveLength(2);

    const octocatRow = requireTestId(container, "identity-row-octocat");
    expect(
      octocatRow.querySelector("[data-testid='token-status-configured']"),
    ).not.toBeNull();
    expect(
      octocatRow.querySelector("[data-testid='token-status-configured']")
        ?.textContent,
    ).toBe("GH auth set");
    expect(
      octocatRow.querySelector("[data-testid='repo-access-ok']"),
    ).not.toBeNull();

    const botRow = requireTestId(container, "identity-row-bot-user");
    expect(
      botRow.querySelector("[data-testid='token-status-missing']"),
    ).not.toBeNull();
    expect(
      botRow.querySelector("[data-testid='token-status-missing']")
        ?.textContent,
    ).toBe("Token missing");
    expect(
      botRow.querySelector("[data-testid='repo-access-unknown']"),
    ).not.toBeNull();
  });

  it("renders source-specific live credential badges", async () => {
    const sidecar = makeSidecar([
      {
        login: "octocat",
        source: "gh_token_login",
        token_status: "auth_timeout",
        repo_access: "check_failed",
      },
      {
        login: "bot-user",
        source: "gh_token_keychain",
        token_status: "token_timeout",
        repo_access: "check_failed",
      },
    ]);

    await render(sidecar);

    expect(
      requireTestId(container, "identity-row-octocat").querySelector(
        "[data-testid='token-status-auth_timeout']",
      )?.textContent,
    ).toBe("GH auth timeout");
    expect(
      requireTestId(container, "identity-row-bot-user").querySelector(
        "[data-testid='token-status-token_timeout']",
      )?.textContent,
    ).toBe("Token timeout");
  });

  it("shows repo access checking state before the live check resolves", async () => {
    let resolveCheck!: (row: IdentityRow) => void;
    const sidecar: IdentitiesSidecar = {
      async list() {
        return [
          {
            login: "octocat",
            source: "gh_token_login",
            token_status: "configured",
            repo_access: "unknown",
          },
        ];
      },
      async checkAccess() {
        return new Promise<IdentityRow>((resolve) => {
          resolveCheck = resolve;
        });
      },
      add: vi.fn(),
      update: vi.fn(),
      remove: vi.fn(),
    };

    await render(sidecar);

    const checkingRow = requireTestId(container, "identity-row-octocat");
    expect(
      checkingRow.querySelector("[data-testid='repo-access-checking']"),
    ).not.toBeNull();

    await act(async () => {
      resolveCheck({
        login: "octocat",
        source: "gh_token_login",
        token_status: "configured",
        repo_access: "ok",
        repo_access_detail: "GitHub repository access verified.",
      });
    });

    const octocatRow = requireTestId(container, "identity-row-octocat");
    expect(
      octocatRow.querySelector("[data-testid='repo-access-ok']"),
    ).not.toBeNull();
  });

  it("shows a row-level repo check failure when the live access check errors", async () => {
    const sidecar: IdentitiesSidecar = {
      async list() {
        return [
          {
            login: "octocat",
            source: "gh_token_login",
            token_status: "configured",
            repo_access: "unknown",
          },
        ];
      },
      async checkAccess() {
        throw new Error("sidecar response timed out");
      },
      add: vi.fn(),
      update: vi.fn(),
      remove: vi.fn(),
    };

    await render(sidecar);

    await act(async () => {
      await Promise.resolve();
    });

    const octocatRow = requireTestId(container, "identity-row-octocat");
    expect(
      octocatRow.querySelector("[data-testid='repo-access-check_failed']"),
    ).not.toBeNull();
    expect(getTestId(container, "repo-access-detail-octocat")).not.toBeNull();
  });

  it("adds an identity when the form is submitted", async () => {
    const sidecar = makeSidecar([]);
    await render(sidecar);

    // Open the add form
    await act(async () => {
      requireTestId(container, "show-add-form-btn").click();
    });

    // Fill in login using the native value setter so React's input event handler fires
    const loginInput = requireTestId(
      container,
      "add-login-input",
    ) as HTMLInputElement;
    await act(async () => {
      const nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        "value",
      )!.set!;
      nativeSetter.call(loginInput, "newuser");
      loginInput.dispatchEvent(new Event("input", { bubbles: true }));
    });

    // Submit via the form's submit event to avoid relying on button click routing
    const form = requireTestId(
      container,
      "add-identity-form",
    ) as HTMLFormElement;
    await act(async () => {
      form.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true }),
      );
    });

    // After add, form hides and list shows
    expect(sidecar.addCalls).toHaveLength(1);
    expect(sidecar.addCalls[0].tokenSource).toBe("gh_token_login");
    expect(getTestId(container, "add-identity-form")).toBeNull();
    expect(getTestId(container, "identities-list")).not.toBeNull();
  });

  it("validates that login cannot be empty on add", async () => {
    const sidecar = makeSidecar([]);
    await render(sidecar);

    await act(async () => {
      requireTestId(container, "show-add-form-btn").click();
    });

    // Submit without filling in login
    await act(async () => {
      requireTestId(container, "add-submit-btn").click();
    });

    expect(getTestId(container, "add-login-error")).not.toBeNull();
    expect(sidecar.addCalls).toHaveLength(0);
    // Form stays open
    expect(getTestId(container, "add-identity-form")).not.toBeNull();
  });

  it("cancels the add form without mutating sidecar", async () => {
    const sidecar = makeSidecar([]);
    await render(sidecar);

    await act(async () => {
      requireTestId(container, "show-add-form-btn").click();
    });
    expect(getTestId(container, "add-identity-form")).not.toBeNull();

    await act(async () => {
      requireTestId(container, "add-cancel-btn").click();
    });

    expect(getTestId(container, "add-identity-form")).toBeNull();
    expect(sidecar.addCalls).toHaveLength(0);
  });

  it("shows the edit form for a row and saves changes", async () => {
    const sidecar = makeSidecar([
      {
        login: "octocat",
        source: "gh_token_login",
        token_status: "configured",
        repo_access: "ok",
      },
    ]);
    await render(sidecar);

    // Click Edit
    await act(async () => {
      requireTestId(container, "edit-btn-octocat").click();
    });

    expect(getTestId(container, "edit-form-octocat")).not.toBeNull();

    // Change token source
    const select = container.querySelector(
      "[data-testid='edit-form-octocat'] select",
    ) as HTMLSelectElement;
    await act(async () => {
      select.value = "gh_token_env";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });

    // Save
    await act(async () => {
      requireTestId(container, "save-edit-octocat").click();
    });

    expect(sidecar.updateCalls).toHaveLength(1);
    expect(sidecar.updateCalls[0]).toEqual({
      login: "octocat",
      patch: { token_source: "gh_token_env" },
    });
    // Edit form closes
    expect(getTestId(container, "edit-form-octocat")).toBeNull();
  });

  it("cancels the edit form without updating", async () => {
    const sidecar = makeSidecar([
      {
        login: "octocat",
        source: "gh_token_login",
        token_status: "configured",
        repo_access: "ok",
      },
    ]);
    await render(sidecar);

    await act(async () => {
      requireTestId(container, "edit-btn-octocat").click();
    });
    await act(async () => {
      requireTestId(container, "cancel-edit-octocat").click();
    });

    expect(getTestId(container, "edit-form-octocat")).toBeNull();
    expect(sidecar.updateCalls).toHaveLength(0);
  });

  it("removes an identity", async () => {
    const sidecar = makeSidecar([
      {
        login: "octocat",
        source: "gh_token_login",
        token_status: "configured",
        repo_access: "ok",
      },
      {
        login: "bot-user",
        source: "gh_token_env",
        token_status: "missing",
        repo_access: "unknown",
      },
    ]);
    await render(sidecar);

    await act(async () => {
      requireTestId(container, "remove-btn-octocat").click();
    });

    expect(sidecar.removeCalls).toEqual(["octocat"]);
    // Row no longer rendered
    expect(getTestId(container, "identity-row-octocat")).toBeNull();
    // Other row remains
    expect(getTestId(container, "identity-row-bot-user")).not.toBeNull();
  });

  it("shows an error banner when remove fails", async () => {
    const sidecar = makeSidecar([
      {
        login: "octocat",
        source: "gh_token_login",
        token_status: "configured",
        repo_access: "ok",
      },
    ]);
    sidecar.remove = vi.fn(async () => {
      throw new Error("network error");
    });
    await render(sidecar);

    await act(async () => {
      requireTestId(container, "remove-btn-octocat").click();
    });

    expect(getTestId(container, "identities-error")).not.toBeNull();
    expect(getTestId(container, "identities-error")!.textContent).toContain(
      "network error",
    );
  });

  it("reuses an existing Keychain PAT without forcing re-entry", async () => {
    const sidecar = makeSidecar([]);
    const checkKeychain = vi.fn(async (login: string) => ({
      login,
      service: `agentshore/${login}`,
      has_token: true,
    }));
    (sidecar as IdentitiesSidecar).checkKeychain = checkKeychain;
    await render(sidecar);

    await act(async () => {
      requireTestId(container, "show-add-form-btn").click();
    });

    // Switch to the Keychain source and enter a login.
    const sourceSelect = container.querySelector(
      "#add-token-source",
    ) as HTMLSelectElement;
    const loginInput = requireTestId(
      container,
      "add-login-input",
    ) as HTMLInputElement;
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    )!.set!;
    await act(async () => {
      nativeSetter.call(loginInput, "octocat");
      loginInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      sourceSelect.value = "gh_token_keychain";
      sourceSelect.dispatchEvent(new Event("change", { bubbles: true }));
    });

    // Source change with a populated login probes the Keychain.
    expect(checkKeychain).toHaveBeenCalledWith("octocat");
    expect(getTestId(container, "keychain-existing-pat")).not.toBeNull();

    // Submit with a blank PAT — allowed because one is already stored.
    const form = requireTestId(
      container,
      "add-identity-form",
    ) as HTMLFormElement;
    await act(async () => {
      form.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true }),
      );
    });

    expect(getTestId(container, "add-login-error")).toBeNull();
    expect(sidecar.addCalls).toHaveLength(1);
    expect(sidecar.addCalls[0].tokenSource).toBe("gh_token_keychain");
  });

  it("still requires a PAT when none is stored in the Keychain", async () => {
    const sidecar = makeSidecar([]);
    (sidecar as IdentitiesSidecar).checkKeychain = vi.fn(
      async (login: string) => ({
        login,
        service: `agentshore/${login}`,
        has_token: false,
      }),
    );
    await render(sidecar);

    await act(async () => {
      requireTestId(container, "show-add-form-btn").click();
    });

    const loginInput = requireTestId(
      container,
      "add-login-input",
    ) as HTMLInputElement;
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    )!.set!;
    await act(async () => {
      nativeSetter.call(loginInput, "octocat");
      loginInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const sourceSelect = container.querySelector(
      "#add-token-source",
    ) as HTMLSelectElement;
    await act(async () => {
      sourceSelect.value = "gh_token_keychain";
      sourceSelect.dispatchEvent(new Event("change", { bubbles: true }));
    });

    expect(getTestId(container, "keychain-existing-pat")).toBeNull();

    const form = requireTestId(
      container,
      "add-identity-form",
    ) as HTMLFormElement;
    await act(async () => {
      form.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true }),
      );
    });

    // Blank PAT with no stored token is rejected.
    expect(getTestId(container, "add-login-error")).not.toBeNull();
    expect(sidecar.addCalls).toHaveLength(0);
  });

  it("shows an error banner on load failure", async () => {
    const sidecar: IdentitiesSidecar = {
      list: vi.fn(async () => {
        throw new Error("sidecar not available");
      }),
      add: vi.fn(),
      update: vi.fn(),
      remove: vi.fn(),
    };
    await render(sidecar);

    expect(getTestId(container, "identities-error")).not.toBeNull();
  });
});

describe("IdentitiesScreen — agent backend auth", () => {
  it("does not render the section (and does not crash) when the sidecar lacks checkAgentAuth", async () => {
    // makeSidecar omits checkAgentAuth, mirroring an older/mock sidecar.
    const sidecar = makeSidecar([]);
    await render(sidecar);

    expect(getTestId(container, "agent-auth-section")).toBeNull();
    // The rest of the screen still renders fine.
    expect(getTestId(container, "identities-screen")).not.toBeNull();
    expect(getTestId(container, "identities-empty")).not.toBeNull();
  });

  it("renders an error badge and remediation hint for an expired agent backend token", async () => {
    const rows: AgentAuthRow[] = [
      {
        agent_type: "codex",
        status: "expired",
        detail: "Session token expired — run 'codex login' to refresh.",
      },
    ];
    const sidecar = makeSidecar([]);
    const checkAgentAuth = vi.fn(async () => rows);
    (sidecar as IdentitiesSidecar).checkAgentAuth = checkAgentAuth;

    await render(sidecar);

    // The probe runs on mount.
    expect(checkAgentAuth).toHaveBeenCalledTimes(1);

    const section = requireTestId(container, "agent-auth-section");
    expect(section).not.toBeNull();

    const codexRow = requireTestId(container, "agent-auth-row-codex");
    const badge = codexRow.querySelector(
      "[data-testid='agent-auth-status-expired']",
    );
    expect(badge).not.toBeNull();
    expect(badge?.className).toContain("badge-error");
    expect(badge?.textContent).toBe("Backend auth expired");

    const detail = getTestId(container, "agent-auth-detail-codex");
    expect(detail).not.toBeNull();
    expect(detail?.textContent).toContain("codex login");
  });

  it("re-probes when the Verify button is clicked", async () => {
    const sidecar = makeSidecar([]);
    const checkAgentAuth = vi.fn(async (): Promise<AgentAuthRow[]> => [
      { agent_type: "codex", status: "ok", detail: "" },
    ]);
    (sidecar as IdentitiesSidecar).checkAgentAuth = checkAgentAuth;

    await render(sidecar);
    expect(checkAgentAuth).toHaveBeenCalledTimes(1);

    await act(async () => {
      requireTestId(container, "agent-auth-verify-btn").click();
    });

    expect(checkAgentAuth).toHaveBeenCalledTimes(2);
    const badge = requireTestId(container, "agent-auth-row-codex").querySelector(
      "[data-testid='agent-auth-status-ok']",
    );
    expect(badge?.className).toContain("badge-ok");
  });

  it("surfaces an error banner when the agent auth probe fails", async () => {
    const sidecar = makeSidecar([]);
    (sidecar as IdentitiesSidecar).checkAgentAuth = vi.fn(async () => {
      throw new Error("sidecar agents.check_auth timed out");
    });

    await render(sidecar);

    const banner = getTestId(container, "agent-auth-error");
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain("agents.check_auth");
  });
});
