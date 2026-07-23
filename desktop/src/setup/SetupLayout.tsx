import { useCallback, useMemo, type Dispatch, type SetStateAction } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  IdentitiesScreen,
  type IdentitiesSidecar,
  TrustedSourcesScreen,
  type TrustedSourcesSidecar,
} from "@agentshore/dashboard";
import {
  addIdentity,
  addTrustedSource,
  checkIdentityAccess,
  checkKeychainToken,
  listIdentities,
  listTrustedSources,
  removeIdentity,
  removeTrustedSource,
  updateIdentity,
} from "../rpc/identitiesClient";
import { type AgentRow } from "../rpc/agentsClient";
import {
  budgetSelectionToConfig,
  setBudget,
  setSeedPaths,
  setTrustedIssueEnforcement,
} from "../rpc/projectClient";
import { AgentsScreen } from "../screens/AgentsScreen";
import { BudgetScreen } from "../screens/BudgetScreen";
import { ReadinessScreen } from "../screens/ReadinessScreen";
import { StartScreen, type StartSelection } from "../screens/StartScreen";
import { TargetBranchScreen } from "../screens/TargetBranchScreen";
import { SETUP_SCREENS, isSetupScreen, persistSetup, type SetupScreen, type SetupState } from "./setupState";

export function SetupLayout({
  setup,
  setSetup,
  onStart,
  quickStartError,
  onDismissQuickStartError,
}: {
  setup: SetupState;
  setSetup: Dispatch<SetStateAction<SetupState>>;
  onStart: (selection: StartSelection) => void;
  /**
   * Surface a Quick Start failure (issue #565) as a banner above the
   * current step's content. The rail itself stays interactive so the
   * user can fix whatever the regular Setup flow flags.
   */
  quickStartError: { message: string; step: SetupScreen } | null;
  onDismissQuickStartError: () => void;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const currentParts = location.pathname.split("/");
  const currentPart = currentParts[currentParts.length - 1];
  const current = isSetupScreen(currentPart) ? currentPart : "readiness";

  const missingTargetBranch = setup.targetBranch.trim().length === 0;
  const missingAgents = setup.enabledAgents.length < 2;
  const missingIdentities = setup.identities.length < 2;
  const canStart = !missingTargetBranch && !missingAgents && !missingIdentities;

  const flags = useMemo<Record<SetupScreen, boolean>>(
    () => ({
      "target-branch": missingTargetBranch,
      identities: missingIdentities,
      agents: missingAgents,
      // Budget is never a hard blocker — Unlimited is a valid choice and
      // the default — so the rail never flags it as "needs input".
      budget: false,
      readiness: false,
      start: !canStart,
    }),
    [canStart, missingAgents, missingIdentities, missingTargetBranch],
  );

  const identitiesSidecar = useMemo<IdentitiesSidecar>(
    () => ({
      list: listIdentities,
      add: addIdentity,
      update: updateIdentity,
      remove: removeIdentity,
      checkKeychain: checkKeychainToken,
      checkAccess: checkIdentityAccess,
    }),
    [],
  );
  const trustedSourcesSidecar = useMemo<TrustedSourcesSidecar>(
    () => ({
      list: listTrustedSources,
      add: addTrustedSource,
      remove: removeTrustedSource,
    }),
    [],
  );
  const onIdentityRowsChange = useCallback(
    (rows: Array<{ login: string }>) => {
      const identities = rows.map((row) => row.login);
      setSetup((prev) => {
        const unchanged =
          prev.identities.length === identities.length &&
          prev.identities.every((login, index) => login === identities[index]);
        if (unchanged) return prev;
        const next = { ...prev, identities };
        persistSetup(next);
        return next;
      });
    },
    [setSetup],
  );

  const onTrustedSourcesChange = useCallback(
    (logins: string[]) => {
      setSetup((prev) => {
        const unchanged =
          prev.trustedSources.length === logins.length &&
          prev.trustedSources.every((login, index) => login === logins[index]);
        if (unchanged) return prev;
        const next = { ...prev, trustedSources: logins };
        persistSetup(next);
        return next;
      });
    },
    [setSetup],
  );

  const onAgentRowsChange = useCallback(
    (rows: AgentRow[]) => {
      const enabledAgents = rows.filter((r) => r.enabled).map((r) => r.type);
      setSetup((prev) => {
        const unchanged =
          prev.enabledAgents.length === enabledAgents.length &&
          prev.enabledAgents.every((type, i) => type === enabledAgents[i]);
        if (unchanged) return prev;
        const next = { ...prev, enabledAgents };
        persistSetup(next);
        return next;
      });
    },
    [setSetup],
  );

  return (
    <main className="setup-shell">
      <header className="setup-header">
        <div className="setup-header__text">
          <h1>Setup Session</h1>
          <p>Move through each required setup surface before starting AgentShore.</p>
        </div>
        <button
          type="button"
          className="fm-btn fm-btn--secondary setup-header__back"
          onClick={() => navigate("/")}
          data-testid="setup-back-to-chooser"
        >
          ← Back to repo selection
        </button>
      </header>
      <div className="setup-body">
        <aside className="setup-rail" aria-label="Setup steps">
          <h2>Setup Rail</h2>
          <ul className="setup-step-list">
            {SETUP_SCREENS.map((screen) => {
              const active = screen.id === current;
              const flagged = flags[screen.id];
              return (
                <li key={screen.id}>
                  <button
                    type="button"
                    onClick={() => navigate(`/setup/${screen.id}`)}
                    className={`setup-step ${active ? "setup-step--active" : ""} ${
                      flagged ? "setup-step--flagged" : ""
                    }`}
                    aria-current={active ? "step" : undefined}
                  >
                    <span>{screen.label}</span>
                    {flagged && <span className="setup-step__flag">Needs input</span>}
                  </button>
                </li>
              );
            })}
          </ul>
        </aside>
        <section className="setup-content">
          {quickStartError !== null && quickStartError.step === current && (
            <div
              role="alert"
              className="setup-quick-start-banner"
              data-testid="quick-start-failure-banner"
            >
              <span>Quick Start failed: {quickStartError.message}</span>
              <button
                type="button"
                onClick={onDismissQuickStartError}
                aria-label="Dismiss Quick Start error"
                className="setup-quick-start-banner__close"
              >
                ×
              </button>
            </div>
          )}
          {current === "readiness" && <ReadinessScreen />}
          {current === "target-branch" && <TargetBranchScreen />}
          {current === "identities" && (
            <>
              <IdentitiesScreen
                sidecar={identitiesSidecar}
                onRowsChange={onIdentityRowsChange}
              />
              {missingIdentities && (
                <p className="id-screen-hint">
                  At least two identities are required to start.
                </p>
              )}
              <TrustedSourcesScreen
                sidecar={trustedSourcesSidecar}
                initialLogins={setup.trustedSources}
                onSourcesChange={onTrustedSourcesChange}
              />
              <label className="id-screen-toggle">
                <input
                  type="checkbox"
                  checked={setup.trustedIssueEnforcement}
                  onChange={(event) => {
                    const checked = event.target.checked;
                    setSetup((prev) => {
                      const merged = { ...prev, trustedIssueEnforcement: checked };
                      persistSetup(merged);
                      return merged;
                    });
                    void (async () => {
                      try {
                        await setTrustedIssueEnforcement(checked);
                      } catch (error) {
                        // Non-fatal: localStorage already tracks the choice.
                        console.error(
                          "project.set_trusted_issue_enforcement failed",
                          error,
                        );
                      }
                    })();
                  }}
                />
                <span>Only work issues opened by trusted identities</span>
              </label>
              {setup.identities.length === 0 && (
                <p className="id-screen-hint">
                  Enabling this restricts issue pickup to the agents' own
                  identities, so the backlog may shrink.
                </p>
              )}
              <div className="setup-screen-actions">
                <button
                  type="button"
                  className="setup-screen-actions__continue"
                  onClick={() => navigate("/setup/agents")}
                >
                  Continue to Agents
                </button>
              </div>
            </>
          )}
          {current === "agents" && (
            <AgentsScreen
              onAgentRowsChange={onAgentRowsChange}
              footerAction={
                <button
                  type="button"
                  className="setup-screen-actions__continue"
                  onClick={() => navigate("/setup/budget")}
                >
                  Continue to Budget
                </button>
              }
            />
          )}
          {current === "budget" && (
            <BudgetScreen
              selection={setup.budget}
              onChange={(next) => {
                setSetup((prev) => {
                  const merged = { ...prev, budget: next };
                  persistSetup(merged);
                  return merged;
                });
              }}
              onSave={async (next) => {
                // Persist to agentshore.yaml via project.set_budget. localStorage
                // already tracks the selection (the onChange path above) so
                // this is the per-project canonical store (issue #571 follow-up).
                await setBudget(budgetSelectionToConfig(next));
              }}
            />
          )}
          {current === "start" && (
            <StartScreen
              blockers={{
                targetBranch: missingTargetBranch,
                agents: missingAgents,
                identities: missingIdentities,
              }}
              selection={setup.startSelection}
              timelapseAvailable={setup.timelapseInstalled}
              onTimelapseInstalled={() => {
                setSetup((prev) => {
                  const merged = { ...prev, timelapseInstalled: true };
                  persistSetup(merged);
                  return merged;
                });
              }}
              onChange={(next) => {
                setSetup((prev) => {
                  const merged = { ...prev, startSelection: next };
                  persistSetup(merged);
                  return merged;
                });
                // Persist the seed to agentshore.yaml (intake.seed_paths) so
                // Quick Start / CLI / TUI honor it via the engine's
                // _resolve_seed_path fallback — seed is no longer a transient,
                // drop-prone parameter. An empty list clears it.
                void setSeedPaths(next.seedInputPath ? [next.seedInputPath] : []).catch(
                  () => undefined,
                );
              }}
              onStart={(selection) => onStart(selection)}
            />
          )}
        </section>
      </div>
    </main>
  );
}
