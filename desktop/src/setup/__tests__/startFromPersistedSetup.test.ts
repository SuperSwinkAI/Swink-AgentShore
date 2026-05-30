/**
 * Tests for the shared "start a session from the persisted setup" helper.
 *
 * Issue #561 (Repeat button on EndSessionReportScreen) and issue #565
 * (Quick Start tile on ChooseProjectScreen) both consume this helper, so
 * the test surface needs to pin the contract both endpoints rely on:
 *
 * * project.select fires before project.inspect, with the supplied path.
 * * navigate() is called with /starting and the persisted seedInputPath
 *   so StartingProgressRoute fires session.start and shows progress steps.
 * * On any step failure the helper short-circuits and calls onError with
 *   the matching ``failedStep`` label, then resolves cleanly so click
 *   handlers don't see an unhandled rejection.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { NavigateFunction } from "react-router-dom";

import { startSessionFromPersistedSetup } from "../startFromPersistedSetup";
import type { inspectProject, selectProject } from "../../rpc/projectClient";

const STORAGE_KEY = "agentshore.desktop.setup.v1";

// jsdom 27 ships a Storage that doesn't implement ``clear`` in this
// environment; replace it with an in-memory shim scoped to each test so
// we have stable seed/teardown semantics.
function installInMemoryLocalStorage(): void {
  const store = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key) => store.get(key) ?? null,
    key: (idx) => Array.from(store.keys())[idx] ?? null,
    removeItem: (key) => {
      store.delete(key);
    },
    setItem: (key, value) => {
      store.set(key, String(value));
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: shim,
    configurable: true,
    writable: true,
  });
}

function makeInspectResult(rawYaml: string | null = null) {
  return {
    path: "/Users/example/projects/foo",
    repo_identity: { is_git: true },
    branch: null,
    detected_tools: [],
    agentshore_yaml: rawYaml === null ? null : { path: "/agentshore.yaml", raw: rawYaml },
    beads_status: { initialised: true },
    prerequisites: { git: true, bd: true, gh: true },
  };
}

describe("startSessionFromPersistedSetup", () => {
  let selectMock: ReturnType<typeof vi.fn> & typeof selectProject;
  let inspectMock: ReturnType<typeof vi.fn> & typeof inspectProject;
  let navigate: ReturnType<typeof vi.fn> & NavigateFunction;

  beforeEach(() => {
    installInMemoryLocalStorage();
    selectMock = vi.fn(() =>
      Promise.resolve({ path: "/Users/example/projects/foo" }),
    ) as ReturnType<typeof vi.fn> & typeof selectProject;
    inspectMock = vi.fn(() =>
      Promise.resolve(makeInspectResult("budget:\n  enabled: true\n  total: 100\n")),
    ) as ReturnType<typeof vi.fn> & typeof inspectProject;
    navigate = vi.fn() as ReturnType<typeof vi.fn> & NavigateFunction;
  });

  afterEach(() => {
    installInMemoryLocalStorage();
  });

  it("calls select → inspect → navigate in order with the supplied project path", async () => {
    await startSessionFromPersistedSetup("/Users/example/projects/foo", {
      navigate,
      selectProjectImpl: selectMock,
      inspectProjectImpl: inspectMock,
    });

    expect(selectMock).toHaveBeenCalledWith("/Users/example/projects/foo");
    expect(inspectMock).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith("/starting", expect.any(Object));
  });

  it("navigates without sessionStarted so StartingProgressRoute fires the RPC", async () => {
    await startSessionFromPersistedSetup("/Users/example/projects/foo", {
      navigate,
      selectProjectImpl: selectMock,
      inspectProjectImpl: inspectMock,
    });

    expect(navigate).toHaveBeenCalledTimes(1);
    const [path, options] = navigate.mock.calls[0] as [
      string,
      { state?: { sessionStarted?: boolean; seedInputPath?: string | null } },
    ];
    expect(path).toBe("/starting");
    expect(options.state?.sessionStarted).toBeUndefined();
    expect(options.state?.seedInputPath).toBeNull();
  });

  it("reads the persisted seedInputPath from localStorage when present", async () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        startSelection: { seedInputPath: "/Users/example/seed.md" },
      }),
    );

    await startSessionFromPersistedSetup("/Users/example/projects/foo", {
      navigate,
      selectProjectImpl: selectMock,
      inspectProjectImpl: inspectMock,
    });

    expect(navigate).toHaveBeenCalledWith(
      "/starting",
      expect.objectContaining({
        state: expect.objectContaining({
          seedInputPath: "/Users/example/seed.md",
        }),
      }),
    );
  });

  it("honors seedInputPathOverride over localStorage", async () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ startSelection: { seedInputPath: "/from-storage.md" } }),
    );

    await startSessionFromPersistedSetup("/Users/example/projects/foo", {
      navigate,
      seedInputPathOverride: "/from-override.md",
      selectProjectImpl: selectMock,
      inspectProjectImpl: inspectMock,
    });

    const [, options] = navigate.mock.calls[0] as [
      string,
      { state?: { seedInputPath?: string | null } },
    ];
    expect(options.state?.seedInputPath).toBe("/from-override.md");
  });

  it("short-circuits with failedStep=select when project.select throws", async () => {
    const onError = vi.fn();
    selectMock.mockRejectedValueOnce(new Error("no such project"));

    await startSessionFromPersistedSetup("/missing", {
      navigate,
      onError,
      selectProjectImpl: selectMock,
      inspectProjectImpl: inspectMock,
    });

    expect(onError).toHaveBeenCalledWith(expect.any(Error), "select");
    expect(inspectMock).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("short-circuits with failedStep=inspect when project.inspect throws", async () => {
    const onError = vi.fn();
    inspectMock.mockRejectedValueOnce(new Error("malformed yaml"));

    await startSessionFromPersistedSetup("/Users/example/projects/foo", {
      navigate,
      onError,
      selectProjectImpl: selectMock,
      inspectProjectImpl: inspectMock,
    });

    expect(onError).toHaveBeenCalledWith(expect.any(Error), "inspect");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("treats a missing agentshore.yaml as a non-fatal inspect — navigate still runs", async () => {
    inspectMock.mockResolvedValueOnce(makeInspectResult(null));

    await startSessionFromPersistedSetup("/Users/example/projects/foo", {
      navigate,
      selectProjectImpl: selectMock,
      inspectProjectImpl: inspectMock,
    });

    expect(navigate).toHaveBeenCalledWith("/starting", expect.any(Object));
  });
});
