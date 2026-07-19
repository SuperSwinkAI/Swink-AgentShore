import { useState, type JSX } from "react";

import { JsonRpcError } from "../rpc/jsonrpc";
import { designateBeadsMigrator } from "../rpc/sessionClient";
import type { SchemaDriftWarning } from "../services/sidecarEvents";
import styles from "./SchemaDriftBanner.module.css";

export interface SchemaDriftBannerProps {
  /** Null hides the banner entirely — nothing to show. */
  warning: SchemaDriftWarning | null;
  /** "Dismiss" clicked — hide without designating. */
  onDismiss: () => void;
  /** Migration designated successfully — caller clears the warning. */
  onDesignated: () => void;
}

/**
 * Persistent, top-of-shell banner shown when the sidecar reports (via
 * ``$/beads_schema_drift``) that this project's remote-backed beads store is
 * behind its shared schema and couldn't be auto-healed headlessly. Lets the
 * user designate this machine as the migrator, which runs ``bd migrate`` +
 * ``bd dolt push`` over the network remote via ``beads.designate_migrator``.
 */
export function SchemaDriftBanner({
  warning,
  onDismiss,
  onDesignated,
}: SchemaDriftBannerProps): JSX.Element | null {
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  if (warning === null) {
    return null;
  }

  const onDesignate = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await designateBeadsMigrator();
      onDesignated();
    } catch (err) {
      const message =
        err instanceof JsonRpcError || err instanceof Error ? err.message : String(err);
      setSubmitError(message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className={styles.banner} role="alert" data-testid="schema-drift-banner">
      <div className={styles.body}>
        <p className={styles.headline}>
          This machine&apos;s beads store is behind its shared schema and can&apos;t be updated
          automatically.
        </p>
        <p className={styles.detail} data-testid="schema-drift-error">
          {warning.error}
        </p>
        <code className={styles.remediation} data-testid="schema-drift-remediation">
          {warning.remediation}
        </code>
        {submitError !== null && (
          <p className={styles.submitError} role="alert" data-testid="schema-drift-submit-error">
            {submitError}
          </p>
        )}
      </div>
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.button}
          onClick={onDismiss}
          disabled={submitting}
          data-testid="schema-drift-dismiss"
        >
          Dismiss
        </button>
        <button
          type="button"
          className={`${styles.button} ${styles.buttonPrimary}`}
          onClick={() => {
            void onDesignate();
          }}
          disabled={submitting}
          data-testid="schema-drift-designate"
        >
          {submitting ? "Migrating…" : "Designate this machine as the migrator"}
        </button>
      </div>
    </div>
  );
}
