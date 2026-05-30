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
      octocatRow.querySelector("[data-testid='repo-access-ok']"),
    ).not.toBeNull();

    const botRow = requireTestId(container, "identity-row-bot-user");
    expect(
      botRow.querySelector("[data-testid='token-status-missing']"),
    ).not.toBeNull();
    expect(
      botRow.querySelector("[data-testid='repo-access-unknown']"),
    ).not.toBeNull();
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
