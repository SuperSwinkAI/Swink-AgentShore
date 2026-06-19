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
  { detected = [] }: { detected?: string[] } = {},
): AgentsAdapter & {
  listAgents: ReturnType<typeof vi.fn>;
  listIdentities: ReturnType<typeof vi.fn>;
  detectAgents: ReturnType<typeof vi.fn>;
  configureAgent: ReturnType<typeof vi.fn>;
} {
  return {
    listAgents: vi.fn().mockResolvedValue(agents),
    listIdentities: vi.fn().mockResolvedValue(identities),
    detectAgents: vi.fn().mockResolvedValue(detected),
    configureAgent: vi.fn().mockResolvedValue(undefined),
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
    identity: "bot-user",
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
  { login: "bot-user", source: "gh_token_login", token_status: "configured", repo_access: "ok" },
  { login: "review-bot", source: "gh_token_env", token_status: "configured", repo_access: "ok" },
];

describe("AgentsScreen", () => {
  it("renders runners with enabled toggle and tier summary", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES);
    renderScreen(adapter);

    const claudeRow = await screen.findByTestId("agent-row-claude_code");
    expect(within(claudeRow).getByText("Claude Code")).toBeInTheDocument();
    expect(within(claudeRow).getByText(/S×1/)).toBeInTheDocument();
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

  it("shows fleet total capacity across enabled runners", async () => {
    const agents: AgentRow[] = [
      {
        type: "claude_code",
        enabled: true,
        identity: "bot-user",
        tier_models: {
          small: { enabled: true, max: 3 },
          medium: { enabled: true, max: 5 },
          large: { enabled: false, max: 20 },
        },
      },
      {
        type: "codex",
        enabled: true,
        identity: "review-bot",
        tier_models: {
          small: { enabled: false, max: 8 },
          medium: { enabled: true, max: 4 },
          large: { enabled: true },
        },
      },
      {
        type: "antigravity",
        enabled: false,
        identity: "review-bot",
        tier_models: {
          small: { enabled: true, max: 20 },
          medium: { enabled: true, max: 20 },
          large: { enabled: true, max: 20 },
        },
      },
    ];
    const adapter = makeAdapter(agents, IDENTITIES);
    renderScreen(adapter);

    const total = await screen.findByTestId("fleet-total");
    expect(within(total).getByText("Fleet Total")).toBeInTheDocument();
    expect(within(total).getByText("S×3")).toBeInTheDocument();
    expect(within(total).getByText("M×9")).toBeInTheDocument();
    expect(within(total).getByText("L×1")).toBeInTheDocument();
    expect(within(total).getByText("Total 13")).toBeInTheDocument();
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

  it("surfaces supported runners that were checked but not detected", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES, {
      detected: ["claude_code", "codex", "antigravity"],
    });
    renderScreen(adapter);

    expect(await screen.findByTestId("agent-unavailable-grok")).toHaveTextContent(
      "Grok CLI — not detected",
    );
    expect(screen.queryByTestId("agent-unavailable-claude_code")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-unavailable-codex")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-unavailable-antigravity")).not.toBeInTheDocument();
  });

  it("keeps detected-but-unconfigured scaffolding while showing unavailable runners", async () => {
    const adapter = makeAdapter(AGENTS, IDENTITIES, {
      detected: ["claude_code", "codex", "grok"],
    });
    renderScreen(adapter);

    expect(await screen.findByTestId("scaffold-grok")).toHaveTextContent("+ Grok CLI");
    expect(screen.queryByTestId("agent-unavailable-grok")).not.toBeInTheDocument();
    expect(screen.getByTestId("agent-unavailable-antigravity")).toHaveTextContent(
      "Antigravity — not detected",
    );
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
    expect(screen.queryByTestId("agent-unavailable-grok")).not.toBeInTheDocument();
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
    };
    renderScreen(adapter);

    expect(await screen.findByRole("alert")).toHaveTextContent("rpc down");
  });

});
