import { invoke } from "@tauri-apps/api/core";
import logoUrl from "./assets/brand/logo.svg";
import {
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import { flushSync } from "react-dom";
import { Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  DashboardCanvas,
  IdentitiesScreen,
  type IdentitiesSidecar,
  TrustedSourcesScreen,
  type TrustedSourcesSidecar,
} from "@agentshore/dashboard";
import {
  addIdentity,
  addTrustedSource,
  checkKeychainToken,
  listIdentities,
  listTrustedSources,
  removeIdentity,
  removeTrustedSource,
  updateIdentity,
} from "./rpc/identitiesClient";
import {
  configureAgent,
  getAgentsCatalog,
  listAgents,
  type AgentRow,
  type AgentsCatalog,
} from "./rpc/agentsClient";
import {
  inspectProject,
  setBudget,
  setSeedPaths,
  setTrustedIssueEnforcement,
} from "./rpc/projectClient";
import {
  budgetHydrationToSelection,
  budgetSelectionToConfig,
  parseProjectYaml,
} from "./setup/projectYaml";
import {
  esrPayloadFromReadyParams,
  SessionContext,
} from "./services/sessionContext";
import { subscribeCompleted } from "./services/sessionClient";
import {
  subscribeSidecarCrashed,
  subscribeSidecarNotification,
  type SidecarCrashedPayload,
} from "./services/sidecarEvents";
import { DemoDashboardScreen } from "./screens/DemoDashboardScreen";
import { SessionDashboardScreen } from "./screens/SessionDashboardScreen";
import { SessionStartingOverlay } from "./SessionStartingOverlay";
import { EndSessionReportScreen } from "./screens/EndSessionReportScreen";

import { AgentsScreen } from "./screens/AgentsScreen";
import {
  BudgetScreen,
  type BudgetSelection,
} from "./screens/BudgetScreen";
import { ChooseProjectScreen } from "./screens/ChooseProjectScreen";
import {
  FatalErrorScreen,
  type FatalShellInfo,
} from "./screens/FatalErrorScreen";
import { ReadinessScreen } from "./screens/ReadinessScreen";
import { RecoveryScreen } from "./screens/RecoveryScreen";
import { StartScreen, type StartSelection } from "./screens/StartScreen";
import { TargetBranchScreen } from "./screens/TargetBranchScreen";
import { StartingProgressRoute } from "./StartingProgressRoute";
import { listen } from "@tauri-apps/api/event";

type UiState = {
  theme: string;
  lastSelectedTab: string;
  window: {
    x: number;
    y: number;
    width: number;
    height: number;
  } | null;
};

type Tier = "small" | "medium" | "large";

type AgentType = "codex" | "claude_code" | "gemini" | "grok";

interface TierEntry {
  model: string;
  enabled: boolean;
}
type TierPlan = Record<Tier, TierEntry>;

// Recommended defaults and per-agent model lists now come from the sidecar
// (agents.catalog), sourced from agentshore.agents.model_catalog.KNOWN_MODELS and
// agentshore.agents.model_tiers.DEFAULT_MODEL_TIERS — the same canonical data the
// CLI wizard reads. Keep AGENT_LABELS local: it's pure presentation.
const AGENT_LABELS: Record<AgentType, string> = {
  codex: "Codex CLI",
  claude_code: "Claude Code",
  gemini: "Gemini CLI",
  grok: "Grok CLI",
};
const AGENT_TYPES = new Set<string>(["codex", "claude_code", "gemini", "grok"]);
const TIERS: Tier[] = ["small", "medium", "large"];

type SetupState = {
  targetBranch: string;
  enabledAgents: string[];
  identities: string[];
  budget: BudgetSelection;
  startSelection: StartSelection;
  /** Whether the optional timelapse-capture feature is installed (from yaml). */
  timelapseInstalled: boolean;
  /** Gate issue pickup to issues opened by trusted identities
   *  (``trusted_ids.restrict_issues_to_trusted_authors``). */
  trustedIssueEnforcement: boolean;
  /** Trusted-source GitHub logins (``trusted_ids.github_logins``), mirrored
   *  from the sidecar so the panel can pre-paint before its own ``list()``
   *  resolves. The TrustedSourcesScreen still self-loads — this is the
   *  hydration parity copy, not a replacement. */
  trustedSources: string[];
};

type SetupScreen =
  | "readiness"
  | "target-branch"
  | "identities"
  | "agents"
  | "budget"
  | "start";
type ThemeChoice = "system" | "light" | "dark";

const SETUP_STORAGE_KEY = "agentshore.desktop.setup.v1";
const SETUP_SCREENS: Array<{ id: SetupScreen; label: string }> = [
  { id: "readiness", label: "Readiness" },
  { id: "target-branch", label: "Target Branch" },
  { id: "identities", label: "Trusted Identities" },
  { id: "agents", label: "Agents" },
  { id: "budget", label: "Budget" },
  { id: "start", label: "Start" },
];
const SETUP_SCREEN_IDS = new Set<string>(SETUP_SCREENS.map((screen) => screen.id));

const defaultSetupState: SetupState = {
  targetBranch: "main",
  enabledAgents: ["codex", "claude_code"],
  identities: [],
  budget: { mode: "unlimited", total: 0, timeMode: "unlimited", timeMinutes: 1440 },
  startSelection: { seedInputPath: null },
  timelapseInstalled: false,
  trustedIssueEnforcement: false,
  trustedSources: [],
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseBudgetSelection(value: unknown): BudgetSelection {
  if (!isRecord(value)) {
    return defaultSetupState.budget;
  }
  const mode: BudgetSelection["mode"] = value.mode === "capped" ? "capped" : "unlimited";
  const totalRaw = value.total;
  const total =
    typeof totalRaw === "number" && Number.isFinite(totalRaw) && totalRaw >= 0
      ? totalRaw
      : defaultSetupState.budget.total;
  // Time dimension (independent). Older persisted snapshots predate these
  // fields — fall back to the defaults so the wizard stays back-compatible.
  const timeMode: BudgetSelection["timeMode"] =
    value.timeMode === "capped" ? "capped" : "unlimited";
  const timeMinutesRaw = value.timeMinutes;
  const timeMinutes =
    typeof timeMinutesRaw === "number" && Number.isFinite(timeMinutesRaw) && timeMinutesRaw >= 0
      ? timeMinutesRaw
      : defaultSetupState.budget.timeMinutes;
  return { mode, total, timeMode, timeMinutes };
}

function parseStartSelection(value: unknown): StartSelection {
  if (!isRecord(value)) {
    return defaultSetupState.startSelection;
  }
  const seedInputPath =
    typeof value.seedInputPath === "string" && value.seedInputPath.length > 0
      ? value.seedInputPath
      : typeof value.seedFilePath === "string" && value.seedFilePath.length > 0
      ? value.seedFilePath
      : null;
  return { seedInputPath };
}

function isAgentType(value: string): value is AgentType {
  return AGENT_TYPES.has(value);
}

function isSetupScreen(value: string | undefined): value is SetupScreen {
  return value !== undefined && SETUP_SCREEN_IDS.has(value);
}

function normalizeTheme(value: string | undefined): ThemeChoice {
  // Legacy "grid-light"/"grid-dark" stored values (pre-desktop-axub) map
  // onto the renamed "light"/"dark" choices so existing preferences carry over.
  if (value === "dark" || value === "grid-dark") return "dark";
  if (value === "light" || value === "grid-light") return "light";
  return "system";
}

function resolveThemeChoice(value: ThemeChoice): "light" | "dark" {
  if (value !== "system") {
    return value;
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function loadStoredSetup(): SetupState {
  try {
    const raw = localStorage.getItem(SETUP_STORAGE_KEY);
    if (!raw) {
      return defaultSetupState;
    }
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed)) {
      return defaultSetupState;
    }
    return {
      targetBranch:
        typeof parsed.targetBranch === "string" && parsed.targetBranch.length > 0
          ? parsed.targetBranch
          : defaultSetupState.targetBranch,
      enabledAgents: Array.isArray(parsed.enabledAgents)
        ? parsed.enabledAgents.filter((value): value is string => typeof value === "string")
        : defaultSetupState.enabledAgents,
      identities: Array.isArray(parsed.identities)
        ? parsed.identities.filter((value): value is string => typeof value === "string")
        : defaultSetupState.identities,
      budget: parseBudgetSelection(parsed.budget),
      startSelection: parseStartSelection(parsed.startSelection),
      timelapseInstalled: parsed.timelapseInstalled === true,
      trustedIssueEnforcement: parsed.trustedIssueEnforcement === true,
      trustedSources: Array.isArray(parsed.trustedSources)
        ? parsed.trustedSources.filter(
            (value): value is string => typeof value === "string",
          )
        : defaultSetupState.trustedSources,
    };
  } catch {
    return defaultSetupState;
  }
}

function persistSetup(next: SetupState): void {
  localStorage.setItem(SETUP_STORAGE_KEY, JSON.stringify(next));
}

function emptyTierEntry(): TierEntry {
  return { model: "", enabled: true };
}

function emptyTierPlan(): TierPlan {
  return { small: emptyTierEntry(), medium: emptyTierEntry(), large: emptyTierEntry() };
}

function tierPlanFromCatalog(
  catalog: AgentsCatalog | null,
  agentType: AgentType,
): TierPlan {
  if (!catalog) return emptyTierPlan();
  const defaults = catalog.defaults[agentType] ?? {};
  return {
    small: { model: defaults.small?.model ?? "", enabled: true },
    medium: { model: defaults.medium?.model ?? "", enabled: true },
    large: { model: defaults.large?.model ?? "", enabled: true },
  };
}

function AgentConfigScreen() {
  // When reached via /setup/agent-config/:type (the Configure button on
  // AgentsScreen), seed the selected agent from the URL param so the user
  // lands on the row they clicked. When reached via the older standalone
  // /onboarding/agent-config route there is no param; default to codex.
  const { type: typeParam } = useParams<{ type?: string }>();
  const navigate = useNavigate();
  // Back/Save both return to /setup/agents if we got here from the setup
  // flow; otherwise (standalone /onboarding entry) return to the home
  // route. Detect by the presence of the :type param.
  const returnTo = typeParam !== undefined ? "/setup/agents" : "/";
  const initialAgentType: AgentType =
    typeParam !== undefined && isAgentType(typeParam) ? typeParam : "codex";
  const [agentType, setAgentType] = useState<AgentType>(initialAgentType);
  const [catalog, setCatalog] = useState<AgentsCatalog | null>(null);
  const [tierPlan, setTierPlan] = useState<TierPlan>(emptyTierPlan());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Load catalog + persisted config once on mount, then again whenever the
  // selected runner changes. Persisted tier_models wins over the catalog
  // defaults so a user who has already configured the agent in CLI sees
  // their values reflected here.
  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    setSaveError(null);
    void Promise.all([getAgentsCatalog(), listAgents()])
      .then(([cat, rows]) => {
        if (cancelled) return;
        setCatalog(cat);
        const saved = rows.find((row) => row.type === agentType);
        const base = tierPlanFromCatalog(cat, agentType);
        if (saved) {
          for (const tier of TIERS) {
            const savedTier = saved.tier_models[tier];
            if (savedTier === undefined) continue;
            if (typeof savedTier.model === "string" && savedTier.model.length > 0) {
              base[tier].model = savedTier.model;
            }
            if (typeof savedTier.enabled === "boolean") {
              base[tier].enabled = savedTier.enabled;
            }
          }
        }
        setTierPlan(base);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [agentType]);

  const recommended = useMemo(
    () => tierPlanFromCatalog(catalog, agentType),
    [catalog, agentType],
  );
  const modelOptions = useMemo<string[]>(() => {
    if (!catalog) return [];
    return catalog.models[agentType] ?? [];
  }, [catalog, agentType]);

  const updateTierModel = (tier: Tier, model: string) => {
    setTierPlan((prev) => ({ ...prev, [tier]: { ...prev[tier], model } }));
    setSaveError(null);
  };

  const updateTierEnabled = (tier: Tier, enabled: boolean) => {
    setTierPlan((prev) => ({ ...prev, [tier]: { ...prev[tier], enabled } }));
    setSaveError(null);
  };

  const resetToRecommended = () => {
    setTierPlan(recommended);
    setSaveError(null);
  };

  const onSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const tier_models: Record<string, { enabled: boolean; model: string }> = {};
      for (const tier of TIERS) {
        const { model, enabled } = tierPlan[tier];
        // Persist the entry whenever the model is set, even when disabled —
        // that way a tier toggled off keeps its model selection for next
        // time the user re-enables it (matches the CLI wizard's behavior
        // where unticked tiers persist as `enabled: false`).
        if (model.length > 0) {
          tier_models[tier] = { enabled, model };
        }
      }
      await configureAgent(agentType, { tier_models });
      navigate(returnTo);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <main className="agent-config-screen">
      <header className="desktop-screen-header">
        <h1>Agent Config</h1>
        <p>
          Recommended defaults are preselected for each tier. Saved configuration round-trips with
          the <code>agentshore identity</code> CLI wizard via <code>agentshore.yaml</code>.
        </p>
      </header>

      {loadError !== null && (
        <div role="alert" className="agent-config-banner agent-config-banner--error">
          Unable to load model catalog: {loadError}
        </div>
      )}

      <section className="desktop-panel agent-config-card">
        <label className="desktop-label" htmlFor="agent-type-select">
          Runner
        </label>
        <select
          id="agent-type-select"
          className="desktop-select"
          value={agentType}
          onChange={(event) => {
            const nextAgentType = event.target.value;
            if (isAgentType(nextAgentType)) {
              setAgentType(nextAgentType);
            }
          }}
        >
          <option value="codex">{AGENT_LABELS.codex}</option>
          <option value="claude_code">{AGENT_LABELS.claude_code}</option>
          <option value="gemini">{AGENT_LABELS.gemini}</option>
          <option value="grok">{AGENT_LABELS.grok}</option>
        </select>
      </section>

      <section className="desktop-panel agent-config-card">
        <h2>Tier Plan</h2>
        <div className="tier-plan-grid">
          {TIERS.map((tier) => {
            const entry = tierPlan[tier];
            const recommendedEntry = recommended[tier];
            const isRecommended =
              entry.model === recommendedEntry.model && entry.enabled === recommendedEntry.enabled;
            // If the saved model isn't in the catalog (e.g. user typed a
            // custom ID via the CLI), still include it as an option so we
            // don't silently drop their selection.
            const options =
              entry.model.length > 0 && !modelOptions.includes(entry.model)
                ? [entry.model, ...modelOptions]
                : modelOptions;
            return (
              <div
                key={tier}
                className={`tier-plan-row${entry.enabled ? "" : " tier-plan-row--disabled"}`}
              >
                <label className="desktop-label" htmlFor={`${tier}-model-select`}>
                  {tier[0].toUpperCase() + tier.slice(1)} tier
                </label>
                <label
                  className="tier-plan-toggle"
                  data-testid={`tier-toggle-${tier}`}
                  title={`Enable the ${tier} tier for this runner. Disabled tiers keep their saved model but are not instantiated.`}
                >
                  <input
                    type="checkbox"
                    checked={entry.enabled}
                    onChange={(event) => updateTierEnabled(tier, event.target.checked)}
                    disabled={catalog === null}
                  />
                  <span>{entry.enabled ? "Enabled" : "Disabled"}</span>
                </label>
                <select
                  id={`${tier}-model-select`}
                  className="desktop-select"
                  value={entry.model}
                  onChange={(event) => updateTierModel(tier, event.target.value)}
                  disabled={catalog === null || !entry.enabled}
                >
                  {options.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
                <small>
                  {!entry.enabled
                    ? "Tier disabled — agent not instantiated at this size"
                    : isRecommended
                    ? "Recommended default selected"
                    : `Override active; recommended: ${recommendedEntry.model || "—"}`}
                </small>
              </div>
            );
          })}
        </div>
        <div className="agent-config-actions">
          <button
            className="fm-btn fm-btn--secondary"
            type="button"
            onClick={() => navigate(returnTo)}
            disabled={saving}
          >
            Back
          </button>
          <button
            className="fm-btn fm-btn--secondary"
            type="button"
            onClick={resetToRecommended}
            disabled={catalog === null}
          >
            Reset to recommended defaults
          </button>
          <button
            className="fm-btn fm-btn--primary"
            type="button"
            onClick={() => void onSave()}
            disabled={catalog === null || saving}
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
        {saveError !== null && (
          <div role="alert" className="agent-config-banner agent-config-banner--error">
            {saveError}
          </div>
        )}
      </section>
    </main>
  );
}

function SetupLayout({
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
            <>
              <AgentsScreen onAgentRowsChange={onAgentRowsChange} />
              <div className="setup-screen-actions">
                <button
                  type="button"
                  className="setup-screen-actions__continue"
                  onClick={() => navigate("/setup/budget")}
                >
                  Continue to Budget
                </button>
              </div>
            </>
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

export function App() {
  const [theme, setTheme] = useState<ThemeChoice>("system");
  const [setup, setSetup] = useState<SetupState>(defaultSetupState);
  const [quickStartError, setQuickStartError] = useState<
    { message: string; step: SetupScreen } | null
  >(null);
  const navigate = useNavigate();
  const location = useLocation();
  const { setEsr, setLastProjectPath, setSessionStarting } = useContext(SessionContext);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const uiState = await invoke<UiState>("load_ui_state");
        if (cancelled) {
          return;
        }
        setTheme(normalizeTheme(uiState.theme));
        setSetup(loadStoredSetup());
        // Intentionally no auto-navigate based on uiState.lastSelectedTab.
        // Every route except "/" assumes a project handle has been
        // established via ChooseProjectScreen's select() RPC; restoring
        // straight to /setup/readiness or /dashboard skips that
        // selection and leaves the rest of the UI half-initialised.
      } catch {
        setSetup(loadStoredSetup());
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [navigate]);

  useEffect(() => {
    const applyResolvedTheme = () => {
      document.documentElement.dataset.themeMode = theme;
      document.documentElement.dataset.theme = resolveThemeChoice(theme);
    };
    applyResolvedTheme();
    if (theme !== "system") {
      return undefined;
    }
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", applyResolvedTheme);
    return () => {
      mq.removeEventListener("change", applyResolvedTheme);
    };
  }, [theme]);

  useEffect(() => {
    const unsubscribe = subscribeCompleted((payload) => {
      setEsr(payload);
      navigate("/session/esr");
    });
    return unsubscribe;
  }, [navigate, setEsr]);

  // Issue #561: when the engine emits ``$/esr_ready`` (drain.py's
  // embedded-mode replacement for ``webbrowser.open``), navigate the
  // shell to ``/session/esr`` immediately with enough payload to render
  // the generated HTML report. The full ESR payload can still arrive on
  // ``session.completed`` (or ``session.stop``'s RPC response) and
  // overwrite this lightweight placeholder.
  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;
    void subscribeSidecarNotification((payload) => {
      if (cancelled) return;
      if (payload.method !== "$/esr_ready") return;
      const readyPayload = esrPayloadFromReadyParams(payload.params);
      if (readyPayload !== null) {
        setEsr(readyPayload);
      }
      navigate("/session/esr");
    })
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [navigate, setEsr]);

  const [crashPayload, setCrashPayload] = useState<SidecarCrashedPayload | null>(null);
  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;
    void subscribeSidecarCrashed((payload) => {
      if (cancelled) return;
      setCrashPayload(payload);
      navigate("/recovery", { replace: true });
    })
      .then((fn) => {
        if (cancelled) {
          fn();
        } else {
          unlisten = fn;
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [navigate]);

  // DESIGN §2.6 fatal-error surface. Two ways the shell finds out the
  // supervisor failed:
  //  1. Tauri event `app.fatal_error` (emitted from the setup hook the
  //     instant the supervisor returns Err). Useful if the WebView is
  //     already mounted when the failure happens — rare in practice.
  //  2. `get_fatal_shell_state` Tauri command queried on mount. This is
  //     the primary path because the setup hook usually runs before the
  //     React app is ready to receive events.
  const [fatalInfo, setFatalInfo] = useState<FatalShellInfo | null>(null);
  useEffect(() => {
    void invoke<FatalShellInfo | null>("get_fatal_shell_state")
      .then((info) => {
        if (info) {
          setFatalInfo(info);
          navigate("/fatal-error", { replace: true });
        }
      })
      .catch(() => undefined);

    let cancelled = false;
    let unlisten: (() => void) | null = null;
    void listen<FatalShellInfo>("app:fatal_error", (event) => {
      if (cancelled) return;
      setFatalInfo(event.payload);
      navigate("/fatal-error", { replace: true });
    })
      .then((fn) => {
        if (cancelled) {
          fn();
        } else {
          unlisten = fn;
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [navigate]);

  const onThemeChange = (next: string) => {
    const normalized = normalizeTheme(next);
    setTheme(normalized);
    void invoke("set_ui_theme", { theme: normalized }).catch(() => undefined);
  };

  const onProjectSelected = async (_path: string) => {
    // Track the most-recent project path on the session context so the
    // End-Session Report's "Repeat with same settings" chrome button
    // (issue #561) can hand it to ``startSessionFromPersistedSetup``
    // without re-querying project.inspect. Set unconditionally — even if
    // the hydration step below fails, the path is still useful.
    setLastProjectPath(_path);
    // DESIGN §10.1 re-entry policy: previous-session choices pre-populate
    // from agentshore.yaml. project.inspect returns the raw YAML content; we
    // parse the slice the setup rail needs (target_branch, enabled
    // agents, identity logins) and overlay it on top of the persisted
    // localStorage state. Any parse / RPC failure is non-fatal — the
    // user can still edit on the rail.
    try {
      const result = await inspectProject();
      const hydration = parseProjectYaml(result.agentshore_yaml?.raw ?? null);
      const budgetSelection = budgetHydrationToSelection(hydration.budget);
      setSetup((prev) => {
        const next: SetupState = {
          ...prev,
          ...(hydration.targetBranch !== null
            ? { targetBranch: hydration.targetBranch }
            : {}),
          ...(hydration.enabledAgents.length > 0
            ? { enabledAgents: hydration.enabledAgents }
            : {}),
          ...(hydration.identityLogins.length > 0
            ? { identities: hydration.identityLogins }
            : {}),
          ...(budgetSelection !== null ? { budget: budgetSelection } : {}),
          ...(hydration.trustedIssueEnforcement !== null
            ? { trustedIssueEnforcement: hydration.trustedIssueEnforcement }
            : {}),
          ...(hydration.trustedSources.length > 0
            ? { trustedSources: hydration.trustedSources }
            : {}),
          timelapseInstalled: hydration.timelapse?.installed ?? false,
        };
        persistSetup(next);
        return next;
      });
    } catch {
      // intentionally swallowed — fall back to localStorage state
    }
  };

  const onStartSession = (selection: StartSelection) => {
    // The goal: the "Starting your session…" modal must be visible
    // BEFORE the route transition + bringup work runs. Two reasons the
    // naive ``setSessionStarting(true); navigate(...)`` doesn't paint
    // on the click frame on Tauri/WebKit:
    //
    //   1. React batches the state update with the route change.
    //      flushSync forces React's commit but doesn't force the
    //      browser to paint — WebKit often defers the paint behind
    //      microtasks queued by the route change.
    //   2. ``navigate(...)`` synchronously unmounts the StartScreen and
    //      mounts StartingProgressRoute, whose useEffect immediately
    //      schedules an async Tauri event subscription. The paint of
    //      the new route's first commit can win against the paint of
    //      the overlay.
    //
    // Fix: commit the overlay state in flushSync, then defer navigate
    // via double-requestAnimationFrame. The first rAF fires before the
    // next paint; the second fires AFTER that paint. By the time the
    // second rAF callback runs, the overlay has actually been put on
    // screen. Then navigate() runs and starts the heavy work.
    flushSync(() => {
      setSessionStarting(true);
    });
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        navigate("/starting", {
          state: {
            seedInputPath: selection.seedInputPath,
            ...(selection.timelapse !== undefined ? { timelapse: selection.timelapse } : {}),
            // Carry the trusted-issue gate so StartingProgressRoute can
            // reconcile it into agentshore.yaml before session.start reads
            // it. The setup-rail toggle persists via a best-effort RPC and
            // the checkbox hydrates from localStorage, so the file may not
            // match the user's choice — the start path is the authoritative
            // write (the project is active there).
            trustedIssueEnforcement: setup.trustedIssueEnforcement,
          },
        });
      });
    });
  };

  const chromeHiddenRoutes = [
    "/dashboard",
    "/demo",
    "/starting",
    "/session/",
    "/recovery",
    "/fatal-error",
  ];
  const chromeHidden = chromeHiddenRoutes.some((prefix) =>
    location.pathname.startsWith(prefix),
  );

  // Match the bridge SPA's body chrome when the dashboard fills the
  // viewport: use dashboard.css's themed --color-fm-bg.
  useEffect(() => {
    const dashboardRoutes = ["/session/dashboard", "/dashboard", "/demo"];
    const onDashboard = dashboardRoutes.some((route) =>
      location.pathname.startsWith(route),
    );
    document.body.classList.toggle("dashboard-active", onDashboard);
    return () => {
      document.body.classList.remove("dashboard-active");
    };
  }, [location.pathname]);

  // Cmd+Shift+D from anywhere jumps to the demo dashboard (desktop-ooao).
  // Skip-setup mount for iterating on Dashboard UI without going through
  // Choose Project → Readiness → Identities → Agents → Start.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (
        event.shiftKey &&
        (event.metaKey || event.ctrlKey) &&
        (event.key === "D" || event.key === "d")
      ) {
        event.preventDefault();
        navigate("/demo");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [navigate]);

  return (
    <div className={`desktop-shell ${chromeHidden ? "desktop-shell--immersive" : ""}`}>
      <SessionStartingOverlay />
      {!chromeHidden && (
        <nav className="desktop-nav">
          <div className="desktop-nav__brand">
            <img src={logoUrl} alt="" aria-hidden="true" className="desktop-nav__mark" />
            <span>AgentShore</span>
          </div>
          <label className="theme-control" htmlFor="theme-select">
            Theme
          </label>
          <select
            id="theme-select"
            className="desktop-select desktop-select--compact"
            value={theme}
            onChange={(event) => onThemeChange(event.target.value)}
          >
            <option value="system">System</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
        </nav>
      )}
      <Routes>
        <Route
          path="/"
          element={
            <ChooseProjectScreen
              onProjectSelected={onProjectSelected}
              onQuickStartFailed={(path, err, failedStep) => {
                // Surface the failure inside the regular Setup-rail
                // flow so Quick Start is never a dead end (issue #565
                // "edge cases" — missing identity, deleted target
                // branch, drifted agentshore.yaml).
                setQuickStartError({ message: err.message, step: failedStep });
                navigate(`/setup/${failedStep}`);
                // Keep onProjectSelected behavior in sync so the rail
                // hydrates from agentshore.yaml even on the fallback path.
                void onProjectSelected(path);
              }}
            />
          }
        />
        <Route
          path="/setup/:screen"
          element={
            <SetupLayout
              setup={setup}
              setSetup={setSetup}
              onStart={onStartSession}
              quickStartError={quickStartError}
              onDismissQuickStartError={() => setQuickStartError(null)}
            />
          }
        />
        <Route
          path="/dashboard"
          element={
            <main className="dashboard-dev-route">
              <DashboardCanvas />
            </main>
          }
        />
        <Route path="/target-branch" element={<TargetBranchScreen />} />
        <Route path="/onboarding/agent-config" element={<AgentConfigScreen />} />
        <Route path="/setup/agent-config/:type" element={<AgentConfigScreen />} />
        <Route path="/starting" element={<StartingProgressRoute />} />
        <Route path="/session/dashboard" element={<SessionDashboardScreen />} />
        <Route path="/demo" element={<DemoDashboardScreen />} />
        <Route path="/session/esr" element={<EndSessionReportScreen />} />
        <Route path="/recovery" element={<RecoveryScreen payload={crashPayload} />} />
        <Route path="/fatal-error" element={<FatalErrorScreen info={fatalInfo} />} />
      </Routes>
    </div>
  );
}
