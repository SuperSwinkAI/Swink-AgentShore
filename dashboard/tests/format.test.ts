import { describe, expect, it } from "vitest";

import { formatAgentType } from "../src/format";

describe("formatAgentType", () => {
  it("formats Grok as a first-class CLI agent", () => {
    expect(formatAgentType("grok")).toBe("Grok");
  });
});
