import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from "react";
import { useNavigate } from "react-router-dom";

import {
  listBranches,
  setTargetBranch,
  type BranchRow,
} from "../rpc/projectClient";

import styles from "./TargetBranchScreen.module.css";

export interface TargetBranchAdapter {
  list: (refresh: boolean) => Promise<BranchRow[]>;
  setTarget: (name: string) => Promise<{ target_branch: string }>;
}

const defaultAdapter: TargetBranchAdapter = {
  list: listBranches,
  setTarget: setTargetBranch,
};

export interface TargetBranchScreenProps {
  adapter?: TargetBranchAdapter;
}

export function TargetBranchScreen({
  adapter = defaultAdapter,
}: TargetBranchScreenProps): JSX.Element {
  const navigate = useNavigate();
  const [branches, setBranches] = useState<BranchRow[] | null>(null);
  const [selectedBranch, setSelectedBranch] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Track whether the user actually picked a different branch, so the
  // leave-flush (below) never silently overwrites the configured target with
  // the auto-seeded default on a passive visit. ``savedRef`` suppresses a
  // double-write after an explicit "Set target branch" click.
  const dirtyRef = useRef(false);
  const savedRef = useRef(false);
  const selectedBranchRef = useRef("");
  selectedBranchRef.current = selectedBranch;
  const setTargetRef = useRef(adapter.setTarget);
  setTargetRef.current = adapter.setTarget;

  const onSelectBranch = useCallback((name: string) => {
    dirtyRef.current = true;
    setSelectedBranch(name);
  }, []);

  // Persist the selection when the user leaves this screen by ANY exit path.
  // "Set target branch" saves explicitly, but the left rail and Back navigate
  // away without it — and the selection lives only in local state, so without
  // this flush a rail-navigated change is lost entirely (not even localStorage
  // holds it). Only flush a user-made, not-yet-saved selection.
  useEffect(
    () => () => {
      if (savedRef.current || !dirtyRef.current) return;
      const branch = selectedBranchRef.current;
      if (branch) void setTargetRef.current(branch).catch(() => undefined);
    },
    [],
  );

  const sortedBranches = useMemo(() => {
    if (branches === null) return [];
    return [...branches].sort((a, b) => {
      if (a.is_default !== b.is_default) {
        return a.is_default ? -1 : 1;
      }
      return a.name.localeCompare(b.name);
    });
  }, [branches]);

  const loadBranches = useCallback(
    async (refresh: boolean) => {
      setLoading(true);
      setError(null);
      setStatus(null);
      try {
        const rows = await adapter.list(refresh);
        setBranches(rows);
        const nextSelection = rows.find((row) => row.is_default)?.name ?? rows[0]?.name ?? "";
        setSelectedBranch((current) => current || nextSelection);
        setStatus(refresh ? "Branch list refreshed." : "Branch list loaded.");
      } catch (err) {
        setBranches([]);
        setError(`Unable to load branches: ${err instanceof Error ? err.message : String(err)}`);
      } finally {
        setLoading(false);
      }
    },
    [adapter],
  );

  const onSetTarget = useCallback(async () => {
    if (!selectedBranch) {
      setError("Select a branch first.");
      return;
    }
    setSaving(true);
    setError(null);
    setStatus(null);
    try {
      await adapter.setTarget(selectedBranch);
      savedRef.current = true;
      setStatus(`Target branch set to ${selectedBranch}.`);
      navigate("/setup/identities");
    } catch (err) {
      setError(`Unable to set target branch: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  }, [adapter, navigate, selectedBranch]);

  useEffect(() => {
    void loadBranches(false);
  }, [loadBranches]);

  const loaded = branches !== null;

  return (
    <main className={styles.screen} data-testid="target-branch-screen">
      <header className={styles.header}>
        <div className={styles.headerText}>
          <h1>Target branch</h1>
          <p>Select the branch AgentShore treats as trunk for the current project.</p>
        </div>
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.button}
            onClick={() => void loadBranches(true)}
            disabled={loading || saving}
            data-testid="target-branch-refresh"
          >
            {loading ? "Refreshing…" : "Refresh branches"}
          </button>
          <button
            type="button"
            className={`${styles.button} ${styles.buttonPrimary}`}
            onClick={() => void onSetTarget()}
            disabled={saving || !selectedBranch}
            data-testid="target-branch-save"
          >
            {saving ? "Saving…" : "Set target branch"}
          </button>
        </div>
      </header>

      {status !== null && (
        <div className={`${styles.banner} ${styles.bannerOk}`} role="status">
          {status}
        </div>
      )}
      {error !== null && (
        <div className={`${styles.banner} ${styles.bannerError}`} role="alert">
          {error}
        </div>
      )}

      <section className={styles.panel}>
        {!loaded && <p className={styles.empty}>Loading…</p>}
        {loaded && sortedBranches.length === 0 && (
          <p className={styles.empty}>No branches found.</p>
        )}
        {loaded && sortedBranches.length > 0 && (
          <table className={styles.table}>
            <thead>
              <tr>
                <th aria-label="selected"></th>
                <th>Name</th>
                <th>Default</th>
                <th>Current</th>
                <th>Remote only</th>
                <th>Ahead</th>
                <th>Behind</th>
              </tr>
            </thead>
            <tbody>
              {sortedBranches.map((branch) => {
                const key = `${branch.name}-${branch.is_remote ? "remote" : "local"}`;
                const isSelected = selectedBranch === branch.name;
                return (
                  <tr
                    key={key}
                    className={isSelected ? styles.rowSelected : undefined}
                    data-testid={`branch-row-${branch.name}${branch.is_remote ? "-remote" : ""}`}
                  >
                    <td>
                      <input
                        type="radio"
                        name="target-branch"
                        checked={isSelected}
                        onChange={() => onSelectBranch(branch.name)}
                        aria-label={`Select ${branch.name}`}
                      />
                    </td>
                    <td>{branch.name}</td>
                    <td>{branch.is_default ? <span className={styles.yes}>yes</span> : ""}</td>
                    <td>{branch.is_current ? <span className={styles.yes}>yes</span> : ""}</td>
                    <td>{branch.is_remote ? "yes" : ""}</td>
                    <td className={styles.numeric}>{branch.ahead}</td>
                    <td className={styles.numeric}>{branch.behind}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
