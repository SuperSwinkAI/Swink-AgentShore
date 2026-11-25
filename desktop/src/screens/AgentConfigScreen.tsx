import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { AGENT_REGISTRY, AGENT_TYPES } from "@agentshore/dashboard";
import {
  configureAgent,
  getAgentsCatalog,
  listAgents,
  type AgentsCatalog,
} from "../rpc/agentsClient";

type Tier = "small" | "medium" | "large";



type AgentType = keyof typeof AGENT_REGISTRY;

interface TierEntry {
  model: string;
  enabled: boolean;
  max: number;
  reasoning_effort: string | null;
}
type TierPlan = Record<Tier, TierEntry>;





const AGENT_TYPE_SET = new Set<string>(AGENT_TYPES);
const TIERS: Tier[] = ["small", "medium", "large"];

function isAgentType(value: string): value is AgentType {
  return AGENT_TYPE_SET.has(value);
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

export function AgentConfigScreen() {




  const { type: typeParam } = useParams<{ type?: string }>();
  const navigate = useNavigate();



  const returnTo = typeParam !== undefined ? "/setup/agents" : "/";
  const initialAgentType: AgentType =
    typeParam !== undefined && isAgentType(typeParam) ? typeParam : "codex";
  const [agentType, setAgentType] = useState<AgentType>(initialAgentType);
  const [catalog, setCatalog] = useState<AgentsCatalog | null>(null);
  const [tierPlan, setTierPlan] = useState<TierPlan>(emptyTierPlan());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);





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

      const isGrok = agentType === "grok";


      const supportsEffort = effortOptions.length > 0;
      for (const tier of TIERS) {
        const { model, enabled, max, reasoning_effort } = tierPlan[tier];
        const persistedModel = isGrok ? "grok-4.5" : model;




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
                        grok-4.5
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
