import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { inspectProject, type ProjectInspectResult } from "../rpc/projectClient";

import styles from "./ReadinessScreen.module.css";

/**
 * Mirrors the Rust ``readiness::ReadinessFindingKind`` enum
 * (``snake_case`` over the wire). The desktop shell synthesises
 * findings from ``project.inspect``; the Rust types stay authoritative
 * for the ``readiness_hard_blocked`` predicate that ships in
 * ``desktop/src-tauri/src/readiness.rs``.
 */
export type ReadinessFindingKind =
  | "is_agentshore_source_repo"
  | "not_a_git_repository"
  | "github_identity_missing"
  | "beads_not_initialized"
  | "tooling_unavailable"
  | "other";

export interface ReadinessFinding {
  kind: ReadinessFindingKind;
  message: string;
}

/**
 * Hard-blocker kinds must match ``is_hard_blocker`` in
 * desktop/src-tauri/src/readiness.rs. Keep the two in sync.
 */
const HARD_BLOCKERS: ReadinessFindingKind[] = [
  "is_agentshore_source_repo",
  "not_a_git_repository",
];

export function isHardBlocker(kind: ReadinessFindingKind): boolean {
  return HARD_BLOCKERS.includes(kind);
}

export function isFromAgentShoreSourceRepo(originUrl: string | null | undefined): boolean {
  if (!originUrl) return false;
  return /SuperSwinkAI\/(?:Swink-)?AgentShore(\.git)?$/i.test(originUrl);
}

/**
 * Synthesise the Screen 2 finding list from a ``project.inspect``
 * response. The same predicate logic that the Rust hard-blocker check
 * encodes — kept pure so it can be unit-tested in isolation.
 */
export function findingsFromInspect(inspect: ProjectInspectResult): ReadinessFinding[] {
  const findings: ReadinessFinding[] = [];

  if (isFromAgentShoreSourceRepo(inspect.repo_identity.origin_url)) {
    findings.push({
      kind: "is_agentshore_source_repo",
      message:
        "Target path points at the AgentShore source repository. Pick a different project to avoid AgentShore editing its own code.",
    });
  }

  if (!inspect.repo_identity.is_git) {
    findings.push({
      kind: "not_a_git_repository",
      message: `${inspect.path} is not a Git repository. Initialise one with \`git init\` or pick a different project.`,
    });
  }

  const missingTools: string[] = [];
  if (!inspect.prerequisites.git) missingTools.push("git");
  if (!inspect.prerequisites.bd) missingTools.push("bd");
  if (!inspect.prerequisites.gh) missingTools.push("gh");
  if (missingTools.length > 0) {
    findings.push({
      kind: "tooling_unavailable",
      message: `Required tools missing on PATH: ${missingTools.join(", ")}.`,
    });
  }

  if (!inspect.beads_status.initialised) {
    findings.push({
      kind: "beads_not_initialized",
      message: "Beads project graph not initialised yet. AgentShore will run `bd init` during startup.",
    });
  }

  return findings;
}

export interface ReadinessAdapter {
  inspect: () => Promise<ProjectInspectResult>;
}

const defaultAdapter: ReadinessAdapter = {
  inspect: inspectProject,
};

export interface ReadinessScreenProps {
  adapter?: ReadinessAdapter;
}

export function ReadinessScreen({
  adapter = defaultAdapter,
}: ReadinessScreenProps): JSX.Element {
  const navigate = useNavigate();
  const [inspect, setInspect] = useState<ProjectInspectResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await adapter.inspect();
      setInspect(result);
    } catch (err) {
      setError(`Unable to inspect project: ${err instanceof Error ? err.message : String(err)}`);
      setInspect(null);
    } finally {
      setLoading(false);
    }
  }, [adapter]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const findings = useMemo(
    () => (inspect ? findingsFromInspect(inspect) : []),
    [inspect],
  );
  const hardBlocked = useMemo(() => findings.some((f) => isHardBlocker(f.kind)), [findings]);
  const inspectFailed = error !== null;

  return (
    <main className={styles.screen} data-testid="readiness-screen">
      <header className={styles.header}>
        <h1>Readiness</h1>
        <p>
          Safety-only hard blocks. Informational findings will not prevent you from continuing —
          they are surfaced so the next setup screens can offer the right fix.
        </p>
      </header>

      {error !== null && (
        <div className={`${styles.banner} ${styles.bannerError}`} role="alert">
          {error}
        </div>
      )}

      <section className={styles.panel} aria-label="Readiness findings">
        {loading && <p className={styles.empty}>Inspecting project…</p>}
        {!loading && !inspectFailed && findings.length === 0 && (
          <p className={styles.empty} data-testid="readiness-empty">
            All clear — project is ready to set up.
          </p>
        )}
        {!loading &&
          findings.map((finding) => {
            const blocker = isHardBlocker(finding.kind);
            return (
              <div
                key={finding.kind}
                className={`${styles.finding} ${blocker ? styles.findingBlocker : styles.findingInfo}`}
                data-testid={`readiness-finding-${finding.kind}`}
                data-blocker={blocker ? "true" : "false"}
              >
                <span
                  className={`${styles.badge} ${blocker ? styles.badgeBlocker : styles.badgeInfo}`}
                >
                  {blocker ? "Blocker" : "Info"}
                </span>
                <p className={styles.message}>{finding.message}</p>
              </div>
            );
          })}
      </section>

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.button}
          onClick={() => void reload()}
          disabled={loading}
          data-testid="readiness-refresh"
        >
          {loading ? "Refreshing…" : "Re-run checks"}
        </button>
        <button
          type="button"
          className={`${styles.button} ${styles.buttonPrimary}`}
          onClick={() => navigate("/setup/target-branch")}
          disabled={loading || hardBlocked || inspectFailed}
          data-testid="readiness-continue"
        >
          Continue to Target Branch
        </button>
      </div>
    </main>
  );
}
