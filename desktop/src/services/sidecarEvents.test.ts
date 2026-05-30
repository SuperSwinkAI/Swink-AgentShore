import { describe, expect, it, vi } from "vitest";
import {
  subscribeProgress,
  subscribeSidecarCrashed,
  subscribeSidecarNotification,
} from "./sidecarEvents";

const { listenMock } = vi.hoisted(() => ({
  listenMock: vi.fn(),
}));

vi.mock("@tauri-apps/api/event", () => ({
  listen: listenMock,
}));

describe("subscribeSidecarCrashed", () => {
  it("subscribes to sidecar.crashed and forwards payload", async () => {
    const payload = {
      exit_code: 17,
      last_stderr_lines: ["line 1", "line 2"],
      log_file_path: "/tmp/sidecar.log",
    };
    const handler = vi.fn();
    const unlisten = vi.fn();

    listenMock.mockImplementationOnce(async (_event, cb) => {
      cb({ payload });
      return unlisten;
    });

    const returned = await subscribeSidecarCrashed(handler);

    expect(listenMock).toHaveBeenCalledWith("sidecar:crashed", expect.any(Function));
    expect(handler).toHaveBeenCalledWith(payload);
    expect(returned).toBe(unlisten);
  });
});

describe("subscribeSidecarNotification", () => {
  it("subscribes to sidecar:notification and forwards method+params", async () => {
    const payload = {
      method: "$/progress",
      params: { step: "init_beads", status: "running" },
    };
    const handler = vi.fn();
    const unlisten = vi.fn();

    listenMock.mockImplementationOnce(async (_event, cb) => {
      cb({ payload });
      return unlisten;
    });

    const returned = await subscribeSidecarNotification(handler);

    expect(listenMock).toHaveBeenCalledWith("sidecar:notification", expect.any(Function));
    expect(handler).toHaveBeenCalledWith(payload);
    expect(returned).toBe(unlisten);
  });
});

describe("subscribeProgress", () => {
  it("derives status='ok' when percent reaches 100", async () => {
    const handler = vi.fn();
    let cb: (event: { payload: { method: string; params: unknown } }) => void = () => {};
    listenMock.mockImplementationOnce(async (_event, fn) => {
      cb = fn;
      return vi.fn();
    });
    await subscribeProgress(handler);

    cb({
      payload: {
        method: "$/progress",
        params: {
          token: "session-start",
          step: "init_beads",
          percent: 100,
          message: "Beads ready",
        },
      },
    });

    expect(handler).toHaveBeenCalledWith({
      token: "session-start",
      step: "init_beads",
      status: "ok",
      percent: 100,
      message: "Beads ready",
      error: null,
    });
  });

  it("derives status='running' for in-progress percent values", async () => {
    const handler = vi.fn();
    let cb: (event: { payload: { method: string; params: unknown } }) => void = () => {};
    listenMock.mockImplementationOnce(async (_event, fn) => {
      cb = fn;
      return vi.fn();
    });
    await subscribeProgress(handler);

    cb({
      payload: {
        method: "$/progress",
        params: { token: "session-start", step: "install_skills", percent: 40 },
      },
    });

    expect(handler).toHaveBeenCalledWith(
      expect.objectContaining({ step: "install_skills", status: "running", percent: 40 }),
    );
  });

  it("ignores notifications whose method is not $/progress", async () => {
    const handler = vi.fn();
    let cb: (event: { payload: { method: string; params: unknown } }) => void = () => {};
    listenMock.mockImplementationOnce(async (_event, fn) => {
      cb = fn;
      return vi.fn();
    });
    await subscribeProgress(handler);

    cb({
      payload: {
        method: "session.completed",
        params: { reason: "human_stop" },
      },
    });

    expect(handler).not.toHaveBeenCalled();
  });

  it("derives status='failed' and forwards the error string when error is set", async () => {
    const handler = vi.fn();
    let cb: (event: { payload: { method: string; params: unknown } }) => void = () => {};
    listenMock.mockImplementationOnce(async (_event, fn) => {
      cb = fn;
      return vi.fn();
    });
    await subscribeProgress(handler);

    cb({
      payload: {
        method: "$/progress",
        params: { step: "config_merge", percent: 30, error: "yaml parse error" },
      },
    });

    expect(handler).toHaveBeenCalledWith(
      expect.objectContaining({ step: "config_merge", status: "failed", error: "yaml parse error" }),
    );
  });

  it("leaves status undefined when percent is missing and no error is set", async () => {
    const handler = vi.fn();
    let cb: (event: { payload: { method: string; params: unknown } }) => void = () => {};
    listenMock.mockImplementationOnce(async (_event, fn) => {
      cb = fn;
      return vi.fn();
    });
    await subscribeProgress(handler);

    cb({
      payload: {
        method: "$/progress",
        params: { step: "init_beads" },
      },
    });

    expect(handler).toHaveBeenCalledWith(
      expect.objectContaining({ step: "init_beads", status: undefined }),
    );
  });
});
