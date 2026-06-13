import React from "react";
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import { ErrorBoundary } from "../src/index";

/**
 * The dashboard mounts <Dashboard/> with no boundary above it, so a render
 * throw used to unmount the whole root to a blank screen ("crashed on
 * reload"). These tests pin the boundary's two guarantees: pass-through when
 * healthy, and a readable in-DOM fallback (not a blank screen) when a child
 * throws.
 */
function Boom(): JSX.Element {
  throw new Error("kaboom-from-render");
}

describe("ErrorBoundary", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });

  it("renders children unchanged when nothing throws", () => {
    act(() => {
      root.render(
        <ErrorBoundary>
          <div data-testid="ok">healthy</div>
        </ErrorBoundary>,
      );
    });
    expect(container.querySelector('[data-testid="ok"]')?.textContent).toBe(
      "healthy",
    );
  });

  it("catches a render throw and shows the error detail instead of blanking", () => {
    // React logs the caught error to console.error; silence it for a clean run.
    vi.spyOn(console, "error").mockImplementation(() => {});

    act(() => {
      root.render(
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>,
      );
    });

    const alert = container.querySelector('[role="alert"]');
    expect(alert).not.toBeNull();
    // The actual error message is surfaced on-screen (diagnosable, not blank).
    expect(container.textContent).toContain("kaboom-from-render");
  });
});
