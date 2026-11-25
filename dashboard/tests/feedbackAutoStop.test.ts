import { describe, it, expect } from "vitest";

import {
  AUTO_STOP_REASONS,
  DEFAULT_AUTO_STOP_SECONDS,
  shouldAutoStop,
} from "../src/components/FeedbackModal";




describe("FeedbackModal auto-stop policy", () => {
  it("auto-stops automated escalations", () => {
    for (const reason of [
      "loop_detected",
      "stagnation",
      "budget_exhausted",
      "budget_predictive",
    ]) {
      expect(shouldAutoStop(reason)).toBe(true);
      expect(AUTO_STOP_REASONS.has(reason)).toBe(true);
    }
  });

  it("does not auto-stop explicit/unknown pauses", () => {
    for (const reason of ["user_request", "ipc_request", "ambiguous_intake", "", "other"]) {
      expect(shouldAutoStop(reason)).toBe(false);
    }
  });

  it("treats null reason as no auto-stop", () => {
    expect(shouldAutoStop(null)).toBe(false);
  });

  it("defaults to the 120s engine timeout", () => {
    expect(DEFAULT_AUTO_STOP_SECONDS).toBe(120);
  });
});
