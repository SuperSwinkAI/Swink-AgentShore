import { describe, expect, it, beforeEach } from "vitest";
import { getOrCreateReactRoot } from "../src/reactEntry";

describe("getOrCreateReactRoot", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    document.body.className = "";
  });

  it("creates #react-root when absent", () => {
    const root = getOrCreateReactRoot();
    expect(root.id).toBe("react-root");
    expect(document.querySelector("#react-root")).toBe(root);
    expect(document.body.classList.contains("dashboard-active")).toBe(true);
  });

  it("reuses existing #react-root", () => {
    const existing = document.createElement("div");
    existing.id = "react-root";
    document.body.appendChild(existing);

    const root = getOrCreateReactRoot();
    expect(root).toBe(existing);
    expect(document.querySelectorAll("#react-root")).toHaveLength(1);
    expect(document.body.classList.contains("dashboard-active")).toBe(true);
  });
});
