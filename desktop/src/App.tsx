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
  AGENT_REGISTRY,
  AGENT_TYPES,
  DashboardCanvas,
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
} from "./rpc/identitiesClient";
import {
  configureAgent,
  getAgentsCatalog,
  listAgents,
  type AgentRow,
  type AgentsCatalog,
} from "./rpc/agentsClient";
import {
  budgetHydrationToSelection,
  budgetSelectionToConfig,
  inspectProject,
  setBudget,
  setSeedPaths,
  setTrustedIssueEnforcement,
} from "./rpc/projectClient";
import {
  esrPayloadFromReadyParams,
  SessionContext,
} from "./services/sessionContext";
import { subscribeCompleted } from "./services/sessionClient";
import { currentSession } from "./rpc/sessionClient";
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
import { AppMenu } from "./components/AppMenu";
import { WelcomeCarousel } from "./components/WelcomeCarousel";
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
  onboardingCompleted: boolean;
};

type Tier = "small" | "medium" | "large";

// AgentType is imported via AGENT_REGISTRY so the desktop stays in sync with
// the canonical registry in the dashboard package without a second definition.
type AgentType = keyof typeof AGENT_REGISTRY;

interface TierEntry {
  model: string;
  enabled: boolean;
  max: number;
  reasoning_effort: string | null;
}
type TierPlan = Record<Tier, TierEntry>;

// Recommended defaults and per-agent model lists now come from the sidecar
// (agents.catalog), sourced from agentshore.agents.model_catalog.KNOWN_MODELS and
// agentshore.agents.model_tiers.DEFAULT_MODEL_TIERS — the same canonical data the
// CLI wizard reads. Labels are derived from the registry.
const AGENT_TYPE_SET = new Set<string>(AGENT_TYPES);
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
  enabledAgents: ["codex", "claude_code", "antigravity"],
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
  return AGENT_TYPE_SET.has(value);
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
  return { model: "", enabled: true, max: 1, reasoning_effort: null };
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
    small: {
      model: defaults.small?.model ?? "",
      enabled: true,
      max: 1,
      reasoning_effort: defaults.small?.reasoning_effort ?? null,
    },
    medium: {
      model: defaults.medium?.model ?? "",
      enabled: true,
      max: 1,
      reasoning_effort: defaults.medium?.reasoning_effort ?? null,
    },
    large: {
      model: defaults.large?.model ?? "",
      enabled: true,
      max: 1,
      reasoning_effort: defaults.large?.reasoning_effort ?? null,
    },
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
            if (typeof savedTier.max === "number") {
              base[tier].max = savedTier.max;
            }
            if (typeof savedTier.reasoning_effort === "string" && savedTier.reasoning_effort.length > 0) {
              base[tier].reasoning_effort = savedTier.reasoning_effort;
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

  const effortOptions = useMemo<string[]>(() => {
    if (!catalog) return [];
    return catalog.efforts[agentType] ?? [];
  }, [catalog, agentType]);

  const updateTierModel = (tier: Tier, model: string) => {
    setTierPlan((prev) => ({ ...prev, [tier]: { ...prev[tier], model } }));
    setSaveError(null);
  };

  const updateTierEnabled = (tier: Tier, enabled: boolean) => {
    setTierPlan((prev) => ({ ...prev, [tier]: { ...prev[tier], enabled } }));
    setSaveError(null);
  };

  const updateTierMax = (tier: Tier, maxVal: number) => {
    setTierPlan((prev) => ({ ...prev, [tier]: { ...prev[tier], max: maxVal } }));
    setSaveError(null);
  };

  const updateTierEffort = (tier: Tier, value: string) => {
    setTierPlan((prev) => ({
      ...prev,
      [tier]: { ...prev[tier], reasoning_effort: value.length > 0 ? value : null },
    }));
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
      const tier_models: Record<
        string,
        { enabled: boolean; model: string; max: number; reasoning_effort?: string }
      > = {};
      // Grok is hard-pinned to grok-build; never round-trip a stale model id.
      const isGrok = agentType === "grok";
      // Only agents whose CLI exposes an effort flag may persist one — otherwise
      // the backend config validator rejects the whole config.
      const supportsEffort = effortOptions.length > 0;
      for (const tier of TIERS) {
        const { model, enabled, max, reasoning_effort } = tierPlan[tier];
        const persistedModel = isGrok ? "grok-build" : model;
        // Persist the entry whenever the model is set, even when disabled —
        // that way a tier toggled off keeps its model selection for next
        // time the user re-enables it (matches the CLI wizard's behavior
        // where unticked tiers persist as `enabled: false`).
        if (persistedModel.length > 0) {
          tier_models[tier] = {
            enabled,
            model: persistedModel,
            max: max ?? 1,
            ...(supportsEffort && reasoning_effort !== null && reasoning_effort.length > 0
              ? { reasoning_effort }
              : {}),
          };
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
          {AGENT_TYPES.map((type) => (
            <option key={type} value={type}>
              {AGENT_REGISTRY[type].label}
            </option>
          ))}
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
                <div className="tier-plan-row__head">
                  <span className="tier-plan-row__title">
                    {tier[0].toUpperCase() + tier.slice(1)} tier
                  </span>
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
                </div>
                <div className="tier-plan-row__fields">
                  <div className="tier-plan-field tier-plan-field--model">
                    <label className="desktop-label" htmlFor={`${tier}-model-select`}>
                      Model
                    </label>
                    {agentType === "grok" ? (
                      <span
                        id={`${tier}-model-select`}
                        className="desktop-select desktop-select--readonly"
                        aria-label={`Model for ${tier} tier`}
                      >
                        grok-build
                      </span>
                    ) : (
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
                    )}
                  </div>
                  {effortOptions.length > 0 && (
                    <div className="tier-plan-field tier-plan-field--effort">
                      <label className="desktop-label" htmlFor={`${tier}-effort-select`}>
                        Reasoning effort
                      </label>
                      <select
                        id={`${tier}-effort-select`}
                        className="desktop-select"
                        value={entry.reasoning_effort ?? ""}
                        onChange={(event) => updateTierEffort(tier, event.target.value)}
                        disabled={catalog === null || !entry.enabled}
                        aria-label={`Reasoning effort for ${tier} tier`}
                        data-testid={`tier-effort-${tier}`}
                      >
                        <option value="">— Default —</option>
                        {effortOptions.map((effort) => (
                          <option key={effort} value={effort}>
                            {effort}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                  <div className="tier-plan-field tier-plan-field--max">
                    <label className="desktop-label" htmlFor={`${tier}-max-input`}>
                      Max agents
                    </label>
                    <input
                      id={`${tier}-max-input`}
                      type="number"
                      min={1}
                      max={20}
                      step={1}
                      value={entry.max ?? 1}
                      disabled={catalog === null || !entry.enabled}
                      onChange={(event) =>
                        updateTierMax(
                          tier,
                          Math.min(20, Math.max(1, parseInt(event.target.value, 10) || 1)),
                        )
                      }
                      aria-label={`Max agents for ${tier} tier`}
                      data-testid={`tier-max-${tier}`}
                    />
                  </div>
                </div>
                <small className="tier-plan-row__hint">
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

export function App() {
  const [theme, setTheme] = useState<ThemeChoice>("system");
  const [setup, setSetup] = useState<SetupState>(defaultSetupState);
  const [quickStartError, setQuickStartError] = useState<
    { message: string; step: SetupScreen } | null
  >(null);
  // First-run welcome carousel. `onboardingSeen` mirrors the persisted
  // `onboarding_completed` flag (Tauri store); `welcomeOpen` is the live
  // visibility. They diverge on replay (Help ▸ Welcome Tour opens it without
  // touching the flag) and on early-close (hidden but not yet seen).
  const [welcomeOpen, setWelcomeOpen] = useState(false);
  const [onboardingSeen, setOnboardingSeen] = useState(true);
  const navigate = useNavigate();
  const location = useLocation();
  const {
    setEsr,
    setLastProjectPath,
    setSessionStarting,
    setDashboardUrl,
    sessionReattaching,
    setSessionReattaching,
  } = useContext(SessionContext);

  // Declare these early so the reattach effect can gate on them.
  const [crashPayload, setCrashPayload] = useState<SidecarCrashedPayload | null>(null);
  const [fatalInfo, setFatalInfo] = useState<FatalShellInfo | null>(null);

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
        // First launch (flag absent/false) → show the welcome carousel over
        // ChooseProjectScreen. The "Don't show again" checkbox mirrors the flag.
        setOnboardingSeen(uiState.onboardingCompleted);
        setWelcomeOpen(!uiState.onboardingCompleted);
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

  // On mount, probe for a session that survived a WebView reload (#274).
  // Precedence: fatal > sidecar-crash > reattach > picker.
  // Fatal/crash effects also use replace:true, so the last one to resolve
  // wins — but we skip the reattach navigation when those states are
  // already set (they resolve from the same Rust state machine, usually
  // faster than a live-session query).
  useEffect(() => {
    let cancelled = false;
    currentSession()
      .then((info) => {
        if (cancelled) return;
        // Respect precedence: skip navigation if a fatal or crash redirect
        // has already been queued.
        if (fatalInfo !== null || crashPayload !== null) return;
        if (info.active && info.dashboardUrl) {
          setDashboardUrl(info.dashboardUrl);
          navigate("/session/dashboard", { replace: true });
          setWelcomeOpen(false);
        }
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setSessionReattaching(false);
      });
    return () => {
      cancelled = true;
    };
  // fatalInfo and crashPayload are intentionally excluded: they're read
  // as a one-shot precedence gate at the moment of resolution, not as
  // reactive dependencies (the effect must run only on mount).
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navigate, setDashboardUrl, setSessionReattaching, setWelcomeOpen]);

  // Replay: Help ▸ Welcome Tour re-opens the carousel without mutating the
  // persisted flag (only the carousel's own checkbox / reaching-the-end does).
  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;
    void listen("menu:welcome_tour", () => {
      if (!cancelled) {
        setWelcomeOpen(true);
      }
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
  }, []);

  // Persist the welcome flag. `markWelcomeSeen` is fired when the user reaches
  // the last slide; it only writes on the false→true transition so reaching the
  // end repeatedly doesn't spam IPC. `setWelcomeSeen` backs the checkbox, which
  // can flip the flag either way (unchecking on replay resumes auto-show).
  const markWelcomeSeen = useCallback(() => {
    setOnboardingSeen((prev) => {
      if (!prev) {
        void invoke("set_onboarding_completed", { completed: true }).catch(
          () => undefined,
        );
      }
      return true;
    });
  }, []);
  const setWelcomeSeen = useCallback((next: boolean) => {
    setOnboardingSeen(next);
    void invoke("set_onboarding_completed", { completed: next }).catch(
      () => undefined,
    );
  }, []);

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

  // Phase 2 — heartbeat (#274). Fires invoke("ui_heartbeat") on mount and
  // every ~2s to let the Rust watchdog detect a JS-alive paint wedge.
  // Each beat is scheduled from within a requestAnimationFrame callback so
  // the beat STOPS if rAF stalls (the only JS-observable signal of a paint
  // wedge). A bare setInterval would keep firing even through a wedge.
  useEffect(() => {
    let mounted = true;
    let rafId: ReturnType<typeof requestAnimationFrame> | undefined;
    let timerId: ReturnType<typeof setTimeout> | undefined;

    const scheduleBeat = () => {
      rafId = requestAnimationFrame(() => {
        if (!mounted) return;
        void invoke("ui_heartbeat").catch(() => undefined);
        timerId = setTimeout(scheduleBeat, 2000);
      });
    };

    scheduleBeat();

    return () => {
      mounted = false;
      if (rafId !== undefined) cancelAnimationFrame(rafId);
      if (timerId !== undefined) clearTimeout(timerId);
    };
  }, []);

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
  // DESIGN §2.6 fatal-error surface. Two ways the shell finds out the
  // supervisor failed:
  //  1. Tauri event `app.fatal_error` (emitted from the setup hook the
  //     instant the supervisor returns Err). Useful if the WebView is
  //     already mounted when the failure happens — rare in practice.
  //  2. `get_fatal_shell_state` Tauri command queried on mount. This is
  //     the primary path because the setup hook usually runs before the
  //     React app is ready to receive events.
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
    // from agentshore.yaml. project.inspect returns typed parsed fields from
    // the Python sidecar's real config loader — no TS YAML parser needed.
    // Any RPC failure is non-fatal — the user can still edit on the rail.
    try {
      const result = await inspectProject();
      const parsed = result.agentshore_yaml?.parsed;
      const budgetSelection = budgetHydrationToSelection(parsed?.budget);
      setSetup((prev) => {
        const next: SetupState = {
          ...prev,
          ...(parsed?.target_branch != null
            ? { targetBranch: parsed.target_branch }
            : {}),
          ...(parsed != null && parsed.enabled_agents.length > 0
            ? { enabledAgents: parsed.enabled_agents }
            : {}),
          ...(parsed != null && parsed.identity_logins.length > 0
            ? { identities: parsed.identity_logins }
            : {}),
          ...(budgetSelection !== null ? { budget: budgetSelection } : {}),
          ...(parsed != null
            ? { trustedIssueEnforcement: parsed.trusted_issue_enforcement }
            : {}),
          ...(parsed != null && parsed.trusted_sources.length > 0
            ? { trustedSources: parsed.trusted_sources }
            : {}),
          timelapseInstalled: parsed?.timelapse_installed ?? false,
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
  // Also hide chrome while the reattach probe is pending: the immersive
  // class makes the "/" route show a blank splash rather than the picker.
  const chromeHidden =
    sessionReattaching ||
    chromeHiddenRoutes.some((prefix) => location.pathname.startsWith(prefix));

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
      <AppMenu />
      <WelcomeCarousel
        open={welcomeOpen}
        seen={onboardingSeen}
        onSeen={markWelcomeSeen}
        onSeenChange={setWelcomeSeen}
        onClose={() => setWelcomeOpen(false)}
      />
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
            // No-flash: while the reattach probe is pending, render null
            // so the immersive splash (desktop-shell--immersive, driven by
            // chromeHidden above) shows instead of the project picker.
            sessionReattaching ? null : (
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
            )
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
