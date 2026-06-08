import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AgentsScreen, type AgentsAdapter } from "../AgentsScreen";
import type { AgentRow } from "../../rpc/agentsClient";
import type { IdentityRow } from "../../rpc/identitiesClient";

function makeAdapter(
  agents: AgentRow[],
  identities: IdentityRow[],
  { maxPerConfig = 2, detected = [] }: { maxPerConfig?: number; detected?: string[] } = {},
): AgentsAdapter & {
  listAgents: ReturnType<typeof vi.fn>;
  listIdentities: ReturnType<typeof vi.fn>;
  detectAgents: ReturnType<typeof vi.fn>;
  configureAgent: ReturnType<typeof vi.fn>;
  getSpawnLimits: ReturnType<typeof vi.fn>;
  setSpawnLimits: ReturnType<typeof vi.fn>;
} {
  return {
    listAgents: vi.fn().mockResolvedValue(agents),
    listIdentities: vi.fn().mockResolvedValue(identities),
    detectAgents: vi.fn().mockResolvedValue(detected),
    configureAgent: vi.fn().mockResolvedValue(undefined),
    getSpawnLimits: vi.fn().mockResolvedValue({ max_per_config: maxPerConfig }),
    setSpawnLimits: vi.fn().mockResolvedValue(undefined),
  };
}

function renderScreen(adapter: AgentsAdapter) {
  return render(
    <MemoryRouter initialEntries={["/setup/agents"]}>
      <Routes>
        <Route path="/setup/agents" element={<AgentsScreen adapter={adapter} />} />
        <Route
          path="/setup/agent-config/:type"
          element={<div data-testid="agent-config-sentinel">cfg</div>}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const AGENTS: AgentRow[] = [
  {
    type: "claude_code",
    enabled: true,
    identity: "unseriousai",
    tier_models: {
      small: { enabled: true, model: "claude-haiku-4-5" },
      medium: { enabled: true, model: "claude-sonnet-4-6" },
      large: { enabled: false },
    },
  },
  {
    type: "codex",
    enabled: false,
    identity: null,
    tier_models: {
      medium: { enabled: true, model: "gpt-5.3-codex" },
    },
  },
];

const IDENTITIES: IdentityRow[] = [
  { login: "unseriousai", source: "gh_token_login", token_status: "configured", repo_access: "ok" },
  { login: "review-bot", source: "gh_token_env", token_status: "configured", repo_access: "ok" },
];

describe("AgentsScreen", () => {
  it("renders runners with enabled toggle and tier summary", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES);
    renderScreen(adapter);

    const claudeRow = await screen.findByTestId("agent-row-claude_code");
    expect(within(claudeRow).getByText("Claude Code")).toBeInTheDocument();
    expect(within(claudeRow).getByText(/small: claude-haiku-4-5/)).toBeInTheDocument();
    expect(within(claudeRow).getByTestId("agent-enabled-claude_code")).toBeChecked();

    const codexRow = screen.getByTestId("agent-row-codex");
    expect(within(codexRow).getByTestId("agent-enabled-codex")).not.toBeChecked();
  });

  it("shows the enabled count chip", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES);
    renderScreen(adapter);

    await waitFor(() => expect(adapter.listAgents).toHaveBeenCalled());
    expect(await screen.findByTestId("agents-enabled-count")).toHaveTextContent("1 enabled");
  });

  it("calls configureAgent({enabled}) when the toggle is clicked", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES);
    renderScreen(adapter);
    const user = userEvent.setup();

    const toggle = await screen.findByTestId("agent-enabled-codex");
    await user.click(toggle);

    await waitFor(() =>
      expect(adapter.configureAgent).toHaveBeenCalledWith("codex", { enabled: true }),
    );
  });

  it("calls configureAgent({identity}) when the select changes", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES);
    renderScreen(adapter);
    const user = userEvent.setup();

    await screen.findByTestId("agent-row-codex");
    const dropdown = screen.getByTestId("agent-identity-codex");
    await user.selectOptions(dropdown, "review-bot");

    await waitFor(() =>
      expect(adapter.configureAgent).toHaveBeenCalledWith("codex", { identity: "review-bot" }),
    );
  });

  it("clears identity to null when the user selects the unassigned option", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES);
    renderScreen(adapter);
    const user = userEvent.setup();

    await screen.findByTestId("agent-row-claude_code");
    const dropdown = screen.getByTestId("agent-identity-claude_code");
    await user.selectOptions(dropdown, "");

    await waitFor(() =>
      expect(adapter.configureAgent).toHaveBeenCalledWith("claude_code", { identity: null }),
    );
  });

  it("navigates to the per-type agent-config route when Configure is clicked", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES);
    renderScreen(adapter);
    const user = userEvent.setup();

    const button = await screen.findByTestId("agent-configure-claude_code");
    await user.click(button);

    expect(await screen.findByTestId("agent-config-sentinel")).toBeInTheDocument();
  });

  it("renders an empty state when no runners are configured", async () => {
    const adapter = makeAdapter([], IDENTITIES);
    renderScreen(adapter);

    expect(await screen.findByText(/No agent runners configured/)).toBeInTheDocument();
  });

  it("labels detected Grok runners for scaffolding", async () => {
    const adapter = makeAdapter([], IDENTITIES, { detected: ["grok"] });
    renderScreen(adapter);

    expect(await screen.findByTestId("scaffold-grok")).toHaveTextContent("+ Grok CLI");
  });

  it("renders configured agents while PATH detection is still pending", async () => {
    const detection = deferred<string[]>();
    const adapter = makeAdapter(AGENTS, IDENTITIES);
    adapter.detectAgents.mockReturnValue(detection.promise);
    renderScreen(adapter);

    expect(await screen.findByTestId("agent-row-claude_code")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    detection.resolve(["grok"]);
  });

  it("does not fail the Agents screen when PATH detection rejects", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES);
    adapter.detectAgents.mockRejectedValue(new Error("PATH probe stalled"));
    renderScreen(adapter);

    expect(await screen.findByTestId("agent-row-claude_code")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("flags a missing identity bound to an agent with a (missing) option", async () => {
    const orphan: AgentRow = {
      ...AGENTS[0],
      identity: "deleted-bot",
    };
    const adapter = makeAdapter([orphan], IDENTITIES);
    renderScreen(adapter);

    const dropdown = await screen.findByTestId("agent-identity-claude_code");
    expect(within(dropdown).getByText(/deleted-bot \(missing\)/)).toBeInTheDocument();
  });

  it("does not render identity selectors for api agents", async () => {
    const apiAgent: AgentRow = {
      type: "api_gpt",
      enabled: true,
      identity: null,
      tier_models: {
        medium: { enabled: true, model: "gpt-5.2" },
      },
    };
    const adapter = makeAdapter([apiAgent], IDENTITIES);
    renderScreen(adapter);

    await screen.findByTestId("agent-row-api_gpt");
    expect(screen.queryByTestId("agent-identity-api_gpt")).not.toBeInTheDocument();
    expect(screen.getByTestId("agent-identity-unsupported-api_gpt")).toHaveTextContent(
      /Not supported/,
    );
  });

  it("surfaces error when listAgents rejects", async () => {
    const adapter: AgentsAdapter = {
      listAgents: vi.fn().mockRejectedValue(new Error("rpc down")),
      listIdentities: vi.fn().mockResolvedValue(IDENTITIES),
      detectAgents: vi.fn().mockResolvedValue([]),
      configureAgent: vi.fn(),
      getSpawnLimits: vi.fn().mockResolvedValue({ max_per_config: 2 }),
      setSpawnLimits: vi.fn(),
    };
    renderScreen(adapter);

    expect(await screen.findByRole("alert")).toHaveTextContent("rpc down");
  });

  it("renders the persisted max-agents-per-type value", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES, { maxPerConfig: 3 });
    renderScreen(adapter);
    const input = await screen.findByTestId("agents-max-per-type");
    expect(input).toHaveValue(3);
  });

  it("persists max-agents-per-type via setSpawnLimits on blur", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES);
    renderScreen(adapter);
    const input = await screen.findByTestId("agents-max-per-type");
    await userEvent.clear(input);
    await userEvent.type(input, "4");
    await userEvent.tab();
    await waitFor(() => {
      expect(adapter.setSpawnLimits).toHaveBeenCalledWith({ max_per_config: 4 });
    });
  });

  it("rejects out-of-range max-agents-per-type and snaps back", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES, { maxPerConfig: 2 });
    renderScreen(adapter);
    const input = await screen.findByTestId("agents-max-per-type");
    await userEvent.clear(input);
    await userEvent.type(input, "99");
    await userEvent.tab();
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/between 1 and 32/);
    });
    expect(input).toHaveValue(2);
    expect(adapter.setSpawnLimits).not.toHaveBeenCalled();
  });
});
