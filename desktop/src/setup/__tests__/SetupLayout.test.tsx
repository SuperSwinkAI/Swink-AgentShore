import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

vi.mock("@agentshore/dashboard", () => ({
  IdentitiesScreen: ({
    sidecar,
  }: {
    sidecar: { checkAgentAuth?: () => Promise<unknown> };
  }) => (
    <div data-testid="identities-agent-auth-adapter">
      {typeof sidecar.checkAgentAuth}
    </div>
  ),
  TrustedSourcesScreen: () => null,
}));

vi.mock("../../rpc/identitiesClient", () => ({
  addIdentity: vi.fn(),
  addTrustedSource: vi.fn(),
  checkIdentityAccess: vi.fn(),
  checkKeychainToken: vi.fn(),
  listIdentities: vi.fn(),
  listTrustedSources: vi.fn(),
  removeIdentity: vi.fn(),
  removeTrustedSource: vi.fn(),
  updateIdentity: vi.fn(),
}));

vi.mock("../../rpc/agentsClient", () => ({
  checkAgentAuth: vi.fn(),
}));

vi.mock("../../rpc/projectClient", () => ({
  budgetSelectionToConfig: vi.fn(),
  setBudget: vi.fn(),
  setSeedPaths: vi.fn(),
  setTrustedIssueEnforcement: vi.fn(),
}));

vi.mock("../../screens/AgentsScreen", () => ({ AgentsScreen: () => null }));
vi.mock("../../screens/BudgetScreen", () => ({ BudgetScreen: () => null }));
vi.mock("../../screens/ReadinessScreen", () => ({ ReadinessScreen: () => null }));
vi.mock("../../screens/StartScreen", () => ({ StartScreen: () => null }));
vi.mock("../../screens/TargetBranchScreen", () => ({
  TargetBranchScreen: () => null,
}));

import { defaultSetupState } from "../setupState";
import { SetupLayout } from "../SetupLayout";

describe("SetupLayout", () => {
  it("wires the production backend-auth probe into IdentitiesScreen", () => {
    render(
      <MemoryRouter initialEntries={["/setup/identities"]}>
        <SetupLayout
          setup={defaultSetupState}
          setSetup={vi.fn()}
          onStart={vi.fn()}
          quickStartError={null}
          onDismissQuickStartError={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("identities-agent-auth-adapter")).toHaveTextContent(
      "function",
    );
  });
});
