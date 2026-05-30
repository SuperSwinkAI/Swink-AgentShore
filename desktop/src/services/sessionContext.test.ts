import { describe, expect, it } from "vitest";

import { esrPayloadFromReadyParams } from "./sessionContext";

describe("esrPayloadFromReadyParams", () => {
  it("builds a renderable ESR payload from $/esr_ready report_path", () => {
    const reportPath =
      "/Users/example/projects/example-project/.agentshore/reports/end-session-2fe32e6d-2eca-4a08-869c-15987c477043.html";

    const payload = esrPayloadFromReadyParams({
      session_id: "2fe32e6d-2eca-4a08-869c-15987c477043",
      archive_path:
        "/Users/example/projects/example-project/.agentshore/archives/2fe32e6d-2eca-4a08-869c-15987c477043",
      report_path: reportPath,
      log_path:
        "/Users/example/projects/example-project/.agentshore/logs/agentshore-2fe32e6d-2eca-4a08-869c-15987c477043.log",
    });

    expect(payload).toEqual(
      expect.objectContaining({
        session_id: "2fe32e6d-2eca-4a08-869c-15987c477043",
        exit_reason: "report_ready",
        archive_path:
          "/Users/example/projects/example-project/.agentshore/archives/2fe32e6d-2eca-4a08-869c-15987c477043",
        report_path: reportPath,
        log_path:
          "/Users/example/projects/example-project/.agentshore/logs/agentshore-2fe32e6d-2eca-4a08-869c-15987c477043.log",
      }),
    );
  });

  it("returns null for malformed ready params", () => {
    expect(esrPayloadFromReadyParams({ session_id: "sid" })).toBeNull();
    expect(
      esrPayloadFromReadyParams({ session_id: "sid", report_path: "/tmp/report.html" }),
    ).toBeNull();
    expect(esrPayloadFromReadyParams(null)).toBeNull();
  });
});
