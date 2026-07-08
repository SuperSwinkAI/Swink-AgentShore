import { useCallback, useEffect, useState, type JSX, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import { AGENT_REGISTRY, AGENT_TYPES, agentLabel } from "@agentshore/dashboard";
import {
  configureAgent,
  detectAgents,
  listAgents,
  refreshModels,
  type AgentConfigurePatch,
  type AgentRow,
  type ModelRefreshSummary,
  type RefreshModelsParams,
} from "../rpc/agentsClient";
import { listIdentities, type IdentityRow } from "../rpc/identitiesClient";

import styles from "./AgentsScreen.module.css";

const TIER_ORDER = [
  ["small", "S"],
  ["medium", "M"],
  ["large", "L"],
] as const;

function tierSummary(row: AgentRow): string {
  const parts = TIER_ORDER
    .map((tier) => {
      const [tierName, initial] = tier;
      const t = row.tier_models[tierName];
      if (!t) return null;
      if (t.enabled === false) return `${initial}✗`;
      const maxVal = t.max ?? 1;
      return `${initial}×${maxVal}`;
    })
    .filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : "no tiers";
}

function tierCapacity(row: AgentRow, tier: string): number {
  if (!row.enabled) return 0;
  const config = row.tier_models[tier];
  if (config === undefined || config.enabled === false) return 0;
  const max = config.max ?? 1;
  if (!Number.isFinite(max)) return 0;
  return Math.max(0, Math.trunc(max));
}

function fleetTotals(rows: AgentRow[]): {
  tiers: { key: string; label: string; total: number }[];
  total: number;
} {
  const tiers = TIER_ORDER.map(([key, label]) => ({
    key,
    label,
    total: rows.reduce((sum, row) => sum + tierCapacity(row, key), 0),
  }));
  return {
    tiers,
    total: tiers.reduce((sum, tier) => sum + tier.total, 0),
  };
}

export interface AgentsAdapter {
  listAgents: () => Promise<AgentRow[]>;
  listIdentities: () => Promise<IdentityRow[]>;
  detectAgents: () => Promise<string[]>;
  configureAgent: (type: string, patch: AgentConfigurePatch) => Promise<void>;
  refreshModels: (params: RefreshModelsParams) => Promise<ModelRefreshSummary>;
}

const defaultAdapter: AgentsAdapter = {
  listAgents,
  listIdentities,
  detectAgents,
  configureAgent,
  refreshModels,
};

// Measured during development against real Claude Code dispatches — see
// docs/design/agents/DESIGN.md "Model Discovery". A successful run costs
// roughly this much; the dispatch itself hard-caps well above it.
const CLAUDE_CODE_REFRESH_COST_ESTIMATE = "roughly $0.30-0.50";

function harnessSummaryLine(agentType: string, result: {
  status: string;
  added: string[];
  removed: string[];
  detail: string;
}): string {
  const label = agentLabel(agentType);
  if (result.status === "ok") {
    if (result.added.length === 0 && result.removed.length === 0) {
      return `${label}: no change`;
    }
    const parts: string[] = [];
    if (result.added.length > 0) parts.push(`+${result.added.join(", ")}`);
    if (result.removed.length > 0) parts.push(`−${result.removed.join(", ")}`);
    return `${label}: ${parts.join(" ")}`;
  }
  if (result.status === "skipped") return `${label}: not refreshed`;
  if (result.status === "unavailable") return `${label}: CLI not found on PATH`;
  if (result.status === "budget_exceeded") return `${label}: hit budget cap before finishing`;
  return `${label}: ${result.status}${result.detail ? ` — ${result.detail}` : ""}`;
}

export interface AgentsScreenProps {
  adapter?: AgentsAdapter;
  footerAction?: ReactNode;
  onAgentRowsChange?: (rows: AgentRow[]) => void;
}

export function AgentsScreen({
  adapter = defaultAdapter,
  footerAction,
  onAgentRowsChange,
}: AgentsScreenProps): JSX.Element {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentRow[] | null>(null);
  const [identities, setIdentities] = useState<IdentityRow[]>([]);
  const [detectedTypes, setDetectedTypes] = useState<string[]>([]);
  const [agentDetectionSucceeded, setAgentDetectionSucceeded] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const [modelRefreshBusy, setModelRefreshBusy] = useState<"free" | "claude" | null>(null);
  const [modelRefreshSummary, setModelRefreshSummary] = useState<ModelRefreshSummary | null>(null);
  const [modelRefreshError, setModelRefreshError] = useState<string | null>(null);
  const [showClaudeConfirm, setShowClaudeConfirm] = useState(false);

  const onRefreshFreeModels = useCallback(async () => {
    setModelRefreshBusy("free");
    setModelRefreshError(null);
    try {
      setModelRefreshSummary(await adapter.refreshModels({}));
    } catch (err) {
      setModelRefreshError(err instanceof Error ? err.message : String(err));
    } finally {
      setModelRefreshBusy(null);
    }
  }, [adapter]);

  const onConfirmClaudeRefresh = useCallback(async () => {
    setShowClaudeConfirm(false);
    setModelRefreshBusy("claude");
    setModelRefreshError(null);
    try {
      setModelRefreshSummary(await adapter.refreshModels({ includeClaudeCode: true }));
    } catch (err) {
      setModelRefreshError(err instanceof Error ? err.message : String(err));
    } finally {
      setModelRefreshBusy(null);
    }
  }, [adapter]);

  const refresh = useCallback(async () => {
    try {
      const [agentRows, identityRows] = await Promise.all([
        adapter.listAgents(),
        adapter.listIdentities(),
      ]);
      setAgents(agentRows);
      onAgentRowsChange?.(agentRows);
      setIdentities(identityRows);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      if (agents === null) {
        setAgents([]);
      }
    }
  }, [adapter, agents, onAgentRowsChange]);

  useEffect(() => {
    let cancelled = false;
    setAgentDetectionSucceeded(false);
    Promise.all([adapter.listAgents(), adapter.listIdentities()])
      .then(([agentRows, identityRows]) => {
        if (cancelled) return;
        setAgents(agentRows);
        onAgentRowsChange?.(agentRows);
        setIdentities(identityRows);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setAgents([]);
        setError(err instanceof Error ? err.message : String(err));
      });
    void adapter
      .detectAgents()
      .then((detected) => {
        if (!cancelled) {
          setDetectedTypes(detected);
          setAgentDetectionSucceeded(true);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setDetectedTypes([]);
          setAgentDetectionSucceeded(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [adapter, onAgentRowsChange]);

  const applyPatch = useCallback(
    async (type: string, patch: AgentConfigurePatch) => {
      setBusy(type);
      try {
        await adapter.configureAgent(type, patch);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(null);
      }
    },
    [adapter, refresh],
  );

  const onToggleEnabled = useCallback(
    (row: AgentRow) => {
      void applyPatch(row.type, { enabled: !row.enabled });
    },
    [applyPatch],
  );

  const onIdentityChange = useCallback(
    (row: AgentRow, value: string) => {
      const patch: AgentConfigurePatch = { identity: value === "" ? null : value };
      void applyPatch(row.type, patch);
    },
    [applyPatch],
  );

  const onConfigure = useCallback(
    (row: AgentRow) => {
      navigate(`/setup/agent-config/${encodeURIComponent(row.type)}`);
    },
    [navigate],
  );

  const onScaffold = useCallback(
    (type: string) => {
      void applyPatch(type, { enabled: true });
    },
    [applyPatch],
  );

  const loaded = agents !== null;
  const isEmpty = loaded && agents.length === 0;
  const enabledCount = loaded ? agents.filter((row) => row.enabled).length : 0;
  const totals = loaded ? fleetTotals(agents) : null;
  const unconfiguredDetected = loaded
    ? detectedTypes.filter((type) => !agents.some((a) => a.type === type))
    : [];
  const detectedTypeSet = new Set(detectedTypes);
  const unavailableSupportedTypes = agentDetectionSucceeded
    ? AGENT_TYPES.filter((type) => !detectedTypeSet.has(type))
    : [];

  return (
    <main className={styles.screen}>
      <header className={styles.header}>
        <div className={styles.headerText}>
          <h1>Agents</h1>
          <p>
            Enable runners, bind a GitHub identity, and jump to model tier configuration. At
            least two runners must be enabled to start a session.
          </p>
        </div>
        <div className={styles.summaryGroup}>
          <div className={styles.summary} data-testid="agents-enabled-count">
            {enabledCount} enabled
          </div>
          {unavailableSupportedTypes.length > 0 && (
            <div
              className={styles.unavailableList}
              aria-label="Supported runners not detected"
            >
              {unavailableSupportedTypes.map((type) => (
                <span
                  key={type}
                  className={styles.unavailableChip}
                  data-testid={`agent-unavailable-${type}`}
                >
                  {agentLabel(type)} — not detected
                </span>
              ))}
            </div>
          )}
        </div>
      </header>

      {error !== null && (
        <div role="alert" className={styles.error}>
          {error}
        </div>
      )}

      <section className={styles.panel}>
        <div className={styles.panelHead}>
          <h2>Agent runners</h2>
          <span className={styles.small}>From agentshore.yaml · agents block</span>
        </div>

        {!loaded && <p>Loading…</p>}

        {isEmpty && (
          <div className={styles.empty}>
            <p>No agent runners configured.</p>
            {detectedTypes.length > 0 ? (
              <>
                <p className={styles.small}>
                  Detected on PATH — click to scaffold:
                </p>
                <div className={styles.detectedList}>
                  {detectedTypes.map((type) => (
                    <button
                      key={type}
                      type="button"
                      className={styles.scaffoldButton}
                      disabled={busy === type}
                      onClick={() => onScaffold(type)}
                      data-testid={`scaffold-${type}`}
                    >
                      {busy === type ? "Adding…" : `+ ${agentLabel(type)}`}
                    </button>
                  ))}
                </div>
              </>
            ) : (
              <p className={styles.small}>
                Install Claude Code, Codex CLI, Grok CLI, or Antigravity CLI (agy), then re-open this screen.
              </p>
            )}
          </div>
        )}

        {loaded && agents.length > 0 && unconfiguredDetected.length > 0 && (
          <div className={styles.detectedList} style={{ marginBottom: 8 }}>
            <span className={styles.small}>Detected on PATH, not yet configured:</span>
            {unconfiguredDetected.map((type) => (
              <button
                key={type}
                type="button"
                className={styles.scaffoldButton}
                disabled={busy === type}
                onClick={() => onScaffold(type)}
                data-testid={`scaffold-${type}`}
              >
                {busy === type ? "Adding…" : `+ ${agentLabel(type)}`}
              </button>
            ))}
          </div>
        )}

        {loaded && agents.length > 0 && (
          <div className={styles.list}>
            {agents.map((row) => {
              const rowBusy = busy === row.type;
              const supportsIdentity = !row.type.startsWith("api_");
              return (
                <div
                  key={row.type}
                  className={styles.row}
                  data-testid={`agent-row-${row.type}`}
                >
                  <div className={styles.rowMain}>
                    <span className={styles.rowLabel}>
                      {agentLabel(row.type)}
                      {(AGENT_REGISTRY as Record<string, { beta?: boolean } | undefined>)[
                        row.type
                      ]?.beta && (
                        <span className={styles.betaBadge} data-testid="agent-beta-badge">
                          Beta
                        </span>
                      )}
                    </span>
                    <span className={styles.rowMeta}>{tierSummary(row)}</span>
                  </div>

                  <label className={styles.toggle}>
                    <input
                      type="checkbox"
                      checked={row.enabled}
                      disabled={rowBusy}
                      onChange={() => onToggleEnabled(row)}
                      aria-label={`Enable ${agentLabel(row.type)}`}
                      data-testid={`agent-enabled-${row.type}`}
                    />
                    <span>{row.enabled ? "Enabled" : "Disabled"}</span>
                  </label>

                  <label className={styles.identity}>
                    <span className={styles.identityLabel}>Identity</span>
                    {supportsIdentity ? (
                      <select
                        value={row.identity ?? ""}
                        disabled={rowBusy}
                        onChange={(event) => onIdentityChange(row, event.target.value)}
                        aria-label={`Identity for ${agentLabel(row.type)}`}
                        data-testid={`agent-identity-${row.type}`}
                      >
                        <option value="">— Unassigned —</option>
                        {identities.map((identity) => (
                          <option key={identity.login} value={identity.login}>
                            {identity.login}
                          </option>
                        ))}
                        {row.identity !== null &&
                          !identities.some((ident) => ident.login === row.identity) && (
                            <option value={row.identity}>{row.identity} (missing)</option>
                          )}
                      </select>
                    ) : (
                      <span
                        className={styles.identityUnsupported}
                        data-testid={`agent-identity-unsupported-${row.type}`}
                      >
                        Not supported for API agents
                      </span>
                    )}
                  </label>

                  <button
                    type="button"
                    className={styles.configureButton}
                    onClick={() => onConfigure(row)}
                    data-testid={`agent-configure-${row.type}`}
                  >
                    Configure
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section className={styles.panel} data-testid="model-catalog-panel">
        <div className={styles.panelHead}>
          <h2>Model catalog</h2>
          <span className={styles.small}>Codex, Grok, and Antigravity refresh for free</span>
        </div>

        {modelRefreshError !== null && (
          <div role="alert" className={styles.error}>
            {modelRefreshError}
          </div>
        )}

        <div className={styles.modelCatalogActions}>
          <button
            type="button"
            className={styles.scaffoldButton}
            disabled={modelRefreshBusy !== null}
            onClick={() => void onRefreshFreeModels()}
            data-testid="refresh-models-button"
          >
            {modelRefreshBusy === "free" ? "Refreshing…" : "Refresh Models"}
          </button>

          {!showClaudeConfirm ? (
            <button
              type="button"
              className={styles.claudeRefreshLink}
              disabled={modelRefreshBusy !== null}
              onClick={() => setShowClaudeConfirm(true)}
              data-testid="refresh-claude-code-models-button"
            >
              Refresh Claude Code models (uses API tokens)
            </button>
          ) : (
            <div className={styles.claudeConfirm} data-testid="refresh-claude-code-confirm">
              <p>
                This dispatches a live Claude Code agent (Haiku tier) that searches the web
                for its current model list. Measured cost is{" "}
                {CLAUDE_CODE_REFRESH_COST_ESTIMATE}, hard-capped well above that ceiling if
                the search runs long.
              </p>
              <div className={styles.claudeConfirmActions}>
                <button
                  type="button"
                  className={styles.claudeConfirmButton}
                  onClick={() => void onConfirmClaudeRefresh()}
                  data-testid="refresh-claude-code-confirm-yes"
                >
                  Continue (spend tokens)
                </button>
                <button
                  type="button"
                  className={styles.configureButton}
                  onClick={() => setShowClaudeConfirm(false)}
                  data-testid="refresh-claude-code-confirm-cancel"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>

        {modelRefreshBusy === "claude" && (
          <p className={styles.small} data-testid="refresh-claude-code-in-progress">
            Dispatching Claude Code agent — this can take up to a couple of minutes…
          </p>
        )}

        {modelRefreshSummary !== null && (
          <div className={styles.modelRefreshResults} data-testid="model-refresh-results">
            {Object.entries(modelRefreshSummary.harnesses).map(([agentType, result]) => (
              <div key={agentType} className={styles.small} data-testid={`refresh-result-${agentType}`}>
                {harnessSummaryLine(agentType, result)}
              </div>
            ))}
            {modelRefreshSummary.unpriced_models.length > 0 && (
              <p className={styles.small}>
                New models without a pricing entry (billed at the default rate):{" "}
                {modelRefreshSummary.unpriced_models.map(([, model]) => model).join(", ")}
              </p>
            )}
            {modelRefreshSummary.total_cost_usd > 0 && (
              <p className={styles.small}>
                Spent this refresh: ${modelRefreshSummary.total_cost_usd.toFixed(4)}
              </p>
            )}
          </div>
        )}
      </section>

      {loaded && (agents.length > 0 || footerAction !== undefined) && (
        <footer className={styles.footer}>
          {agents.length > 0 && totals !== null && (
            <div
              className={styles.fleetTotal}
              data-testid="fleet-total"
              aria-label={`Fleet total ${totals.total} agents`}
            >
              <span className={styles.fleetLabel}>Fleet Total</span>
              <span className={styles.fleetFormula}>
                {totals.tiers.map((tier) => (
                  <span key={tier.key} className={styles.fleetChip}>
                    {tier.label}×{tier.total}
                  </span>
                ))}
              </span>
              <span className={styles.fleetEquals}>=</span>
              <span className={styles.fleetGrand}>Total {totals.total}</span>
            </div>
          )}
          {footerAction !== undefined && (
            <div className={styles.footerAction}>{footerAction}</div>
          )}
        </footer>
      )}
    </main>
  );
}
