import { useEffect, useState } from "react";
import {
  subscribeBeadsSchemaDrift,
  type SchemaDriftWarning,
} from "../services/sidecarEvents";

/**
 * $/beads_schema_drift: a remote-backed beads store fell behind its shared
 * schema and couldn't be auto-healed headlessly. Unlike esr_ready this is
 * not a route change — it's a persistent, dismissable banner shown across
 * every route until the user dismisses it or designates this machine as
 * the migrator.
 */
export function useSchemaDriftWarning(): [
  SchemaDriftWarning | null,
  (next: SchemaDriftWarning | null) => void,
] {
  const [schemaDrift, setSchemaDrift] = useState<SchemaDriftWarning | null>(null);

  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;
    void subscribeBeadsSchemaDrift((warning) => {
      if (cancelled) return;
      setSchemaDrift(warning);
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

  return [schemaDrift, setSchemaDrift];
}
