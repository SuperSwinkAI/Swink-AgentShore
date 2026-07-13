import { createContext, useState, type ReactNode } from "react";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export interface EsrPlayStat {
  play_type: string;
  total: number;
  successful: number;
  failed: number;
  success_rate: number;
  total_cost: number;
  avg_duration_seconds: number;
}

export interface EsrClosedIssue {
  issue_number: number;
  title: string;
  closed_at: string | null;
  labels: string[];
}

export interface EsrControlRejection {
  kind: string;
  play_type: string;
  reason: string;
  count: number;
}

export interface EsrOverview {
  session_id: string;
  duration_seconds: number;
  total_plays: number;
  successful_plays: number;
  failed_plays: number;
  total_cost: number;
  final_alignment: number | null;
  started_at: string;
  ended_at: string | null;
}

export interface EsrSummary {
  overview: EsrOverview;
  repo_url: string | null;
  play_stats: EsrPlayStat[];
  closed_issues: EsrClosedIssue[];
  control_rejections: EsrControlRejection[];
  // Other ESR fields are present on the wire but unused by the screen.
  [key: string]: unknown;
}

export interface EsrPayload {
  session_id: string;
  exit_reason: string;
  exit_code: number;
  archive_path: string;
  report_path: string;
  log_path: string | null;
  /** Path to the rendered timelapse MP4 when capture ran; null otherwise. */
  timelapse_output_path?: string | null;
  esr_summary: EsrSummary;
}

export function esrPayloadFromReadyParams(params: unknown): EsrPayload | null {
  if (!isRecord(params)) {
    return null;
  }
  const sessionId = typeof params.session_id === "string" ? params.session_id : null;
  const archivePath = typeof params.archive_path === "string" ? params.archive_path : null;
  const reportPath = typeof params.report_path === "string" ? params.report_path : null;
  const logPath = typeof params.log_path === "string" ? params.log_path : null;
  if (!sessionId || !archivePath || !reportPath) {
    return null;
  }
  return {
    session_id: sessionId,
    exit_reason: "report_ready",
    exit_code: 0,
    archive_path: archivePath,
    report_path: reportPath,
    log_path: logPath,
    esr_summary: {
      overview: {
        session_id: sessionId,
        duration_seconds: 0,
        total_plays: 0,
        successful_plays: 0,
        failed_plays: 0,
        total_cost: 0,
        final_alignment: null,
        started_at: "",
        ended_at: null,
      },
      repo_url: null,
      play_stats: [],
      closed_issues: [],
      control_rejections: [],
    },
  };
}

export interface SessionContextValue {
  dashboardUrl: string | null;
  esr: EsrPayload | null;
  /**
   * Absolute path to the project the most-recent session ran against.
   * Set from ChooseProjectScreen's selection; consumed by the app menu's
   * re-open-project affordance without having to round-trip through
   * ``project.inspect`` first. Survives across session boundaries.
   */
  lastProjectPath: string | null;
  /**
   * True from Start-button click until the first ``instantiate_agent``
   * play event arrives. Drives the top-level Starting-Session overlay so
   * the click-to-first-agent gap (bringup + WS connect + seed_project's
   * first dispatch — ~20-30s total) shows continuous visual feedback
   * instead of a blank-office "did anything happen?" window.
   */
  sessionStarting: boolean;
  /**
   * True from mount until ``current_session`` resolves (one way or
   * another). While true the shell renders an immersive splash instead of
   * ChooseProjectScreen so a live-session reattach never flashes the
   * project picker. Set to ``true`` by ``SessionProvider`` and cleared by
   * the App-level reattach effect in its ``.finally`` handler.
   */
  sessionReattaching: boolean;
  setDashboardUrl: (url: string | null) => void;
  setEsr: (payload: EsrPayload | null) => void;
  setLastProjectPath: (path: string | null) => void;
  setSessionStarting: (starting: boolean) => void;
  setSessionReattaching: (reattaching: boolean) => void;
}

export const SessionContext = createContext<SessionContextValue>({
  dashboardUrl: null,
  esr: null,
  lastProjectPath: null,
  sessionStarting: false,
  sessionReattaching: false,
  setDashboardUrl: () => undefined,
  setEsr: () => undefined,
  setLastProjectPath: () => undefined,
  setSessionStarting: () => undefined,
  setSessionReattaching: () => undefined,
});

export function SessionProvider({ children }: { children: ReactNode }) {
  const [dashboardUrl, setDashboardUrl] = useState<string | null>(null);
  const [esr, setEsr] = useState<EsrPayload | null>(null);
  const [lastProjectPath, setLastProjectPath] = useState<string | null>(null);
  const [sessionStarting, setSessionStarting] = useState(false);
  // Starts true so the shell shows a splash instead of the project picker
  // until the reattach probe in App resolves. Cleared in .finally.
  const [sessionReattaching, setSessionReattaching] = useState(true);
  return (
    <SessionContext.Provider
      value={{
        dashboardUrl,
        esr,
        lastProjectPath,
        sessionStarting,
        sessionReattaching,
        setDashboardUrl,
        setEsr,
        setLastProjectPath,
        setSessionStarting,
        setSessionReattaching,
      }}
    >
      {children}
    </SessionContext.Provider>
  );
}
