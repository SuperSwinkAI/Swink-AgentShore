/**
 * Unit tests for TrustedSourcesScreen (the no-auth "Trusted sources" panel on
 * the desktop setup wizard's Identities screen).
 *
 * Uses react-dom/client + act directly (no @testing-library/react) to stay
 * consistent with the rest of the dashboard test suite.
 */

import React from "react";
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import {
  TrustedSourcesScreen,
  type TrustedSourcesSidecar,
} from "../src/components/TrustedSourcesScreen";

// ---- helpers ---------------------------------------------------------------

function makeSidecar(logins: string[] = []): TrustedSourcesSidecar & {
  _logins: string[];
  addCalls: string[];
  removeCalls: string[];
} {
  const sidecar = {
    _logins: [...logins],
    addCalls: [] as string[],
    removeCalls: [] as string[],
    async list() {
      return [...sidecar._logins].sort();
    },
    async add(login: string) {
      sidecar.addCalls.push(login);
      const canonical = login.toLowerCase();
      if (!sidecar._logins.includes(canonical)) sidecar._logins.push(canonical);
    },
    async remove(login: string) {
      sidecar.removeCalls.push(login);
      sidecar._logins = sidecar._logins.filter(
        (l) => l !== login.toLowerCase(),
      );
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

async function render(
  sidecar: TrustedSourcesSidecar,
  onSourcesChange?: (logins: string[]) => void,
): Promise<void> {
  await act(async () => {
    root.render(
      <TrustedSourcesScreen
        sidecar={sidecar}
        onSourcesChange={onSourcesChange}
      />,
    );
  });
}

function setInputValue(el: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value",
  )?.set;
  setter?.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

// ---- tests -----------------------------------------------------------------

describe("TrustedSourcesScreen", () => {
  it("resolves to the empty message when no sources are configured", async () => {
    await render(makeSidecar([]));
    expect(getTestId(container, "trusted-sources-loading")).toBeNull();
    expect(getTestId(container, "trusted-sources-empty")).not.toBeNull();
  });

  it("reports loaded logins to the parent", async () => {
    const onSourcesChange = vi.fn();
    await render(makeSidecar(["dependabot[bot]", "octocat"]), onSourcesChange);
    expect(onSourcesChange).toHaveBeenCalledWith([
      "dependabot[bot]",
      "octocat",
    ]);
  });

  it("renders a row per trusted source", async () => {
    await render(makeSidecar(["octocat", "renovate[bot]"]));
    const list = requireTestId(container, "trusted-sources-list");
    expect(list.querySelectorAll("li")).toHaveLength(2);
    expect(getTestId(container, "trusted-source-row-octocat")).not.toBeNull();
    expect(
      getTestId(container, "trusted-source-row-renovate[bot]"),
    ).not.toBeNull();
  });

  it("adds a trusted source via the add form", async () => {
    const sidecar = makeSidecar([]);
    await render(sidecar);

    await act(async () => {
      requireTestId(container, "show-add-trusted-btn").click();
    });
    const input = requireTestId(
      container,
      "add-trusted-login-input",
    ) as HTMLInputElement;
    await act(async () => {
      setInputValue(input, "octocat");
    });
    await act(async () => {
      requireTestId(container, "add-trusted-submit-btn").click();
    });

    expect(sidecar.addCalls).toEqual(["octocat"]);
    expect(getTestId(container, "trusted-source-row-octocat")).not.toBeNull();
  });

  it("shows a validation error when adding a blank login", async () => {
    const sidecar = makeSidecar([]);
    await render(sidecar);

    await act(async () => {
      requireTestId(container, "show-add-trusted-btn").click();
    });
    await act(async () => {
      requireTestId(container, "add-trusted-submit-btn").click();
    });

    expect(getTestId(container, "add-trusted-error")).not.toBeNull();
    expect(sidecar.addCalls).toEqual([]);
  });

  it("removes a trusted source", async () => {
    const sidecar = makeSidecar(["octocat"]);
    await render(sidecar);

    await act(async () => {
      requireTestId(container, "trusted-remove-btn-octocat").click();
    });

    expect(sidecar.removeCalls).toEqual(["octocat"]);
    expect(getTestId(container, "trusted-source-row-octocat")).toBeNull();
  });

  it("pre-paints hydrated initialLogins before the sidecar list() resolves", async () => {
    // A sidecar whose list() never settles during the assertion window —
    // proves the seeded rows paint from the hydrated SetupState mirror, not
    // from the self-load.
    let resolveList!: (logins: string[]) => void;
    const lazySidecar: TrustedSourcesSidecar = {
      list: () =>
        new Promise<string[]>((res) => {
          resolveList = res;
        }),
      add: vi.fn(),
      remove: vi.fn(),
    };

    await act(async () => {
      root.render(
        <TrustedSourcesScreen
          sidecar={lazySidecar}
          initialLogins={["dependabot[bot]", "renovate[bot]"]}
        />,
      );
    });

    // list() is still pending, yet the seeded rows are on screen and there
    // is no blank loading flash.
    expect(getTestId(container, "trusted-sources-loading")).toBeNull();
    expect(getTestId(container, "trusted-sources-list")).not.toBeNull();
    expect(
      getTestId(container, "trusted-source-row-dependabot[bot]"),
    ).not.toBeNull();
    expect(
      getTestId(container, "trusted-source-row-renovate[bot]"),
    ).not.toBeNull();

    // When the sidecar finally resolves, the panel reconciles to its truth.
    await act(async () => {
      resolveList(["octocat"]);
    });
    expect(getTestId(container, "trusted-source-row-octocat")).not.toBeNull();
    expect(getTestId(container, "trusted-source-row-renovate[bot]")).toBeNull();
  });

  it("still shows the loading placeholder when no seed is provided", async () => {
    let resolveList!: (logins: string[]) => void;
    const lazySidecar: TrustedSourcesSidecar = {
      list: () =>
        new Promise<string[]>((res) => {
          resolveList = res;
        }),
      add: vi.fn(),
      remove: vi.fn(),
    };

    await act(async () => {
      root.render(<TrustedSourcesScreen sidecar={lazySidecar} />);
    });

    expect(getTestId(container, "trusted-sources-loading")).not.toBeNull();
    expect(getTestId(container, "trusted-sources-list")).toBeNull();

    await act(async () => {
      resolveList([]);
    });
    expect(getTestId(container, "trusted-sources-loading")).toBeNull();
    expect(getTestId(container, "trusted-sources-empty")).not.toBeNull();
  });
});
