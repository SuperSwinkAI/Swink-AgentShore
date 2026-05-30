import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import {
  ReadinessScreen,
  findingsFromInspect,
  isHardBlocker,
  isFromAgentShoreSourceRepo,
  type ReadinessAdapter,
} from "../ReadinessScreen";
import type { ProjectInspectResult } from "../../rpc/projectClient";

function baseInspect(overrides: Partial<ProjectInspectResult> = {}): ProjectInspectResult {
  return {
    path: "/Users/user/example-repo",
    repo_identity: {
      is_git: true,
      root: "/Users/user/example-repo",
      head_sha: "abc",
      origin_url: "git@github.com:wes/example-repo.git",
      ...(overrides.repo_identity ?? {}),
    },
    branch: "main",
    detected_tools: ["python"],
    agentshore_yaml: null,
    beads_status: { initialised: true, ...(overrides.beads_status ?? {}) },
    prerequisites: { git: true, bd: true, gh: true, ...(overrides.prerequisites ?? {}) },
    ...overrides,
  };
}

function makeAdapter(inspect: ProjectInspectResult): ReadinessAdapter & {
  inspect: ReturnType<typeof vi.fn>;
} {
  return { inspect: vi.fn().mockResolvedValue(inspect) };
}

function renderScreen(adapter: ReadinessAdapter) {
  return render(
    <MemoryRouter initialEntries={["/setup/readiness"]}>
      <Routes>
        <Route path="/setup/readiness" element={<ReadinessScreen adapter={adapter} />} />
        <Route
          path="/setup/target-branch"
          element={<div data-testid="target-branch-sentinel">tb</div>}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("isFromAgentShoreSourceRepo", () => {
  it("matches SuperSwinkAI/Swink-AgentShore origin variants", () => {
    expect(isFromAgentShoreSourceRepo("https://github.com/SuperSwinkAI/Swink-AgentShore")).toBe(true);
    expect(isFromAgentShoreSourceRepo("https://github.com/SuperSwinkAI/Swink-AgentShore.git")).toBe(true);
    expect(isFromAgentShoreSourceRepo("git@github.com:SuperSwinkAI/Swink-AgentShore.git")).toBe(true);
  });

  it("returns false for unrelated origins", () => {
    expect(isFromAgentShoreSourceRepo("git@github.com:wes/example-repo.git")).toBe(false);
    expect(isFromAgentShoreSourceRepo(null)).toBe(false);
    expect(isFromAgentShoreSourceRepo(undefined)).toBe(false);
  });
});

describe("isHardBlocker", () => {
  it("flags only is_agentshore_source_repo and not_a_git_repository", () => {
    expect(isHardBlocker("is_agentshore_source_repo")).toBe(true);
    expect(isHardBlocker("not_a_git_repository")).toBe(true);
    expect(isHardBlocker("github_identity_missing")).toBe(false);
    expect(isHardBlocker("beads_not_initialized")).toBe(false);
    expect(isHardBlocker("tooling_unavailable")).toBe(false);
    expect(isHardBlocker("other")).toBe(false);
  });
});

describe("findingsFromInspect", () => {
  it("returns no findings when project is fully ready", () => {
    expect(findingsFromInspect(baseInspect())).toEqual([]);
  });

  it("flags not_a_git_repository when is_git=false", () => {
    const findings = findingsFromInspect(
      baseInspect({ repo_identity: { is_git: false, origin_url: null } }),
    );
    expect(findings).toContainEqual(
      expect.objectContaining({ kind: "not_a_git_repository" }),
    );
  });

  it("flags is_agentshore_source_repo when origin matches the AgentShore repo", () => {
    const findings = findingsFromInspect(
      baseInspect({
        repo_identity: { is_git: true, origin_url: "git@github.com:SuperSwinkAI/Swink-AgentShore.git" },
      }),
    );
    expect(findings[0].kind).toBe("is_agentshore_source_repo");
  });

  it("flags tooling_unavailable listing only the missing tools", () => {
    const findings = findingsFromInspect(
      baseInspect({ prerequisites: { git: true, bd: false, gh: false } }),
    );
    const tooling = findings.find((f) => f.kind === "tooling_unavailable");
    expect(tooling?.message).toMatch(/bd, gh/);
    expect(tooling?.message).not.toMatch(/git,/);
  });

  it("flags beads_not_initialized as informational", () => {
    const findings = findingsFromInspect(
      baseInspect({ beads_status: { initialised: false } }),
    );
    expect(findings).toContainEqual(
      expect.objectContaining({ kind: "beads_not_initialized" }),
    );
  });
});

describe("ReadinessScreen", () => {
  it("renders the empty / all-clear state when no findings", async () => {
    const adapter = makeAdapter(baseInspect());
    renderScreen(adapter);

    expect(await screen.findByTestId("readiness-empty")).toHaveTextContent(/All clear/);
    expect(screen.getByTestId("readiness-continue")).not.toBeDisabled();
  });

  it("disables Continue when a hard-blocker is present", async () => {
    const adapter = makeAdapter(
      baseInspect({ repo_identity: { is_git: false, origin_url: null } }),
    );
    renderScreen(adapter);

    await screen.findByTestId("readiness-finding-not_a_git_repository");
    expect(screen.getByTestId("readiness-continue")).toBeDisabled();
  });

  it("keeps Continue enabled for informational findings (e.g. beads not initialised)", async () => {
    const adapter = makeAdapter(baseInspect({ beads_status: { initialised: false } }));
    renderScreen(adapter);

    await screen.findByTestId("readiness-finding-beads_not_initialized");
    expect(screen.getByTestId("readiness-continue")).not.toBeDisabled();
  });

  it("navigates to /setup/target-branch when Continue is clicked", async () => {
    const adapter = makeAdapter(baseInspect());
    renderScreen(adapter);
    const user = userEvent.setup();

    await screen.findByTestId("readiness-empty");
    await user.click(screen.getByTestId("readiness-continue"));

    expect(await screen.findByTestId("target-branch-sentinel")).toBeInTheDocument();
  });

  it("surfaces an inline error if inspect() rejects", async () => {
    const adapter: ReadinessAdapter = {
      inspect: vi.fn().mockRejectedValue(new Error("sidecar gone")),
    };
    renderScreen(adapter);

    expect(await screen.findByRole("alert")).toHaveTextContent(/sidecar gone/);
    expect(screen.queryByTestId("readiness-empty")).not.toBeInTheDocument();
    expect(screen.getByTestId("readiness-continue")).toBeDisabled();
  });

  it("Re-run checks calls inspect a second time", async () => {
    const adapter = makeAdapter(baseInspect());
    renderScreen(adapter);
    const user = userEvent.setup();

    await waitFor(() => expect(adapter.inspect).toHaveBeenCalledTimes(1));
    await user.click(screen.getByTestId("readiness-refresh"));
    await waitFor(() => expect(adapter.inspect).toHaveBeenCalledTimes(2));
  });
});
