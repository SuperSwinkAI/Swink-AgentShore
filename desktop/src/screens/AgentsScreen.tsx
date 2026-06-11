import { useCallback, useEffect, useState, type JSX } from "react";
import { useNavigate } from "react-router-dom";

import {
  configureAgent,
  detectAgents,
  listAgents,
  type AgentConfigurePatch,
  type AgentRow,
} from "../rpc/agentsClient";
import { listIdentities, type IdentityRow } from "../rpc/identitiesClient";

import styles from "./AgentsScreen.module.css";

const AGENT_TYPE_LABELS: Record<string, string> = {
  claude_code: "Claude Code",
  codex: "Codex CLI",
  gemini: "Gemini CLI",
  grok: "Grok CLI",
};

function agentLabel(type: string): string {
  return AGENT_TYPE_LABELS[type] ?? type;
}

function tierSummary(row: AgentRow): string {
  const TIER_INITIALS: Record<string, string> = { small: "S", medium: "M", large: "L" };
  const parts = ["small", "medium", "large"]
    .map((tier) => {
      const t = row.tier_models[tier];
      if (!t) return null;
      const initial = TIER_INITIALS[tier] ?? tier[0].toUpperCase();
      if (t.enabled === false) return `${initial}✗`;
      const maxVal = t.max ?? 1;
      return `${initial}×${maxVal}`;
    })
    .filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : "no tiers";
}

export interface AgentsAdapter {
  listAgents: () => Promise<AgentRow[]>;
  listIdentities: () => Promise<IdentityRow[]>;
  detectAgents: () => Promise<string[]>;
  configureAgent: (type: string, patch: AgentConfigurePatch) => Promise<void>;
}

const defaultAdapter: AgentsAdapter = {
  listAgents,
  listIdentities,
  detectAgents,
  configureAgent,
};

export interface AgentsScreenProps {
  adapter?: AgentsAdapter;
  onAgentRowsChange?: (rows: AgentRow[]) => void;
}

export function AgentsScreen({
  adapter = defaultAdapter,
  onAgentRowsChange,
}: AgentsScreenProps): JSX.Element {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentRow[] | null>(null);
  const [identities, setIdentities] = useState<IdentityRow[]>([]);
  const [detectedTypes, setDetectedTypes] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

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
    Promise.all([
      adapter.listAgents(),
      adapter.listIdentities(),
      adapter.detectAgents(),
    ])
      .then(([agentRows, identityRows, detected]) => {
        if (cancelled) return;
        setAgents(agentRows);
        onAgentRowsChange?.(agentRows);
        setIdentities(identityRows);
        setDetectedTypes(detected);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setAgents([]);
        setError(err instanceof Error ? err.message : String(err));
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
  const unconfiguredDetected = loaded
    ? detectedTypes.filter((type) => !agents.some((a) => a.type === type))
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
        <div className={styles.summary} data-testid="agents-enabled-count">
          {enabledCount} enabled
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
                Install Claude Code, Codex CLI, Gemini CLI, or Grok CLI, then re-open this screen.
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
                    <span className={styles.rowLabel}>{agentLabel(row.type)}</span>
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
    </main>
  );
}
