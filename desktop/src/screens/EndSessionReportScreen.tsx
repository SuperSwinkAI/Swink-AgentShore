import { useContext, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { archiveClient, type ArchiveClient, type LogRange } from "../services/archiveClient";
import { SessionContext } from "../services/sessionContext";
import styles from "./EndSessionReportScreen.module.css";

type Tab = "report" | "logs";

interface ReportState {
  html: string;
  sections: { id: string; title: string }[];
}

interface LogsState {
  lines: string[];
  end: number;
}

export interface EndSessionReportScreenProps {
  adapter?: Pick<ArchiveClient, "fetchReportByPath" | "fetchLogsByPath">;
  /**
   * Overridable seam for opening the rendered timelapse MP4. Production uses
   * the Tauri ``open_path_in_default_app`` command (same as RecoveryScreen).
   */
  openPathImpl?: (path: string) => Promise<void>;
}

async function defaultOpenPath(path: string): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("open_path_in_default_app", { path });
}

export function EndSessionReportScreen({
  adapter,
  openPathImpl = defaultOpenPath,
}: EndSessionReportScreenProps = {}) {
  const { esr } = useContext(SessionContext);
  const navigate = useNavigate();
  const client = adapter ?? archiveClient;

  const [activeTab, setActiveTab] = useState<Tab>("report");
  const [report, setReport] = useState<ReportState | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [logs, setLogs] = useState<LogsState | null>(null);
  const [logsError, setLogsError] = useState<string | null>(null);
  const [logsLoading, setLogsLoading] = useState(false);
  const reportPath = esr?.report_path ? esr.report_path : null;
  const logPath = esr?.log_path ? esr.log_path : null;
  const timelapsePath = esr?.timelapse_output_path ? esr.timelapse_output_path : null;

  // Auto-open the rendered timelapse MP4 once, when the completed session
  // produced one. Keyed on the path so it fires exactly once per video.
  useEffect(() => {
    if (timelapsePath === null) return;
    void openPathImpl(timelapsePath).catch(() => undefined);
  }, [timelapsePath, openPathImpl]);

  useEffect(() => {
    setReport(null);
    setReportError(null);
  }, [reportPath]);

  useEffect(() => {
    setLogs(null);
    setLogsError(null);
  }, [logPath]);

  useEffect(() => {
    if (activeTab !== "report" || !reportPath || report !== null) {
      return;
    }
    let cancelled = false;
    setReportLoading(true);
    setReportError(null);
    client
      .fetchReportByPath(reportPath)
      .then((result) => {
        if (!cancelled) {
          setReport(result);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setReportError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setReportLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [activeTab, client, report, reportPath]);

  useEffect(() => {
    if (activeTab !== "logs" || !logPath || logs !== null) {
      return;
    }
    let cancelled = false;
    setLogsLoading(true);
    setLogsError(null);
    const requestLogs = (range?: LogRange) =>
      client.fetchLogsByPath(logPath, range);
    requestLogs()
      .then((result) => {
        if (!cancelled) {
          setLogs({ lines: result.lines, end: result.lines.length });
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setLogsError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLogsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [activeTab, client, logPath, logs]);

  const chromeBar = (
    <div className={styles.chrome} data-testid="esr-chrome-bar">
      <div className={styles.chromeLeft}>
        <button
          type="button"
          className={styles.chromeButton}
          onClick={() => navigate("/")}
          aria-label="Back to Home"
        >
          ← Back to Home
        </button>
      </div>
      <button
        type="button"
        className={styles.chromeButton}
        onClick={() => navigate("/")}
      >
        Start a new session
      </button>
      <div className={styles.chromeRight}>
        {timelapsePath !== null && (
          <button
            type="button"
            className={styles.chromeButton}
            onClick={() => {
              void openPathImpl(timelapsePath).catch(() => undefined);
            }}
            aria-label="Open timelapse video"
            data-testid="esr-open-timelapse"
          >
            Open timelapse
          </button>
        )}
      </div>
    </div>
  );

  if (!esr) {
    return (
      <main className={styles.fallback}>
        {chromeBar}
        <h1>No end-of-session report available</h1>
        <Link to="/">Return to start</Link>
      </main>
    );
  }

  const loadMoreLogs = () => {
    if (!logs || logsLoading) {
      return;
    }
    if (!logPath) {
      setLogsError("Log path unavailable.");
      return;
    }
    setLogsLoading(true);
    const range = { start: logs.end + 1, end: logs.end + 200 };
    const requestLogs = (nextRange: LogRange) => client.fetchLogsByPath(logPath, nextRange);
    requestLogs(range)
      .then((result) => {
        setLogs({ lines: [...logs.lines, ...result.lines], end: logs.end + result.lines.length });
      })
      .catch((err: unknown) => {
        setLogsError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setLogsLoading(false));
  };

  const retryReport = () => {
    setReport(null);
    setReportError(null);
  };

  return (
    <main className={styles.page}>
      {chromeBar}
      <div role="tablist" className={styles.tablist}>
        {(["report", "logs"] as const).map((tab) => (
          <button
            key={tab}
            role="tab"
            aria-selected={activeTab === tab}
            className={activeTab === tab ? styles.tabActive : styles.tab}
            onClick={() => setActiveTab(tab)}
            type="button"
          >
            {tab === "report" ? "Full report" : "Raw logs"}
          </button>
        ))}
      </div>
      <section role="tabpanel" className={styles.panel}>
        {activeTab === "report" && (
          <div className={styles.reportPane}>
            {reportLoading && <p className={styles.statusText}>Loading report…</p>}
            {reportError && (
              <div className={styles.errorBlock}>
                <p role="alert">Failed to load report: {reportError}</p>
                <button type="button" onClick={retryReport}>
                  Retry
                </button>
              </div>
            )}
            {!reportLoading && !reportError && reportPath === null && (
              <p className={styles.statusText}>Report path unavailable.</p>
            )}
            {report && (
              <iframe
                className={styles.reportFrame}
                title="Full session report"
                sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"
                srcDoc={report.html}
              />
            )}
          </div>
        )}
        {activeTab === "logs" && (
          <div>
            {logsLoading && logs === null && <p className={styles.statusText}>Loading logs…</p>}
            {logsError && <p role="alert">Failed to load logs: {logsError}</p>}
            {!logsLoading && !logsError && logPath === null && (
              <p className={styles.statusText}>Log path unavailable.</p>
            )}
            {logs && (
              <>
                <pre className={styles.logs}>{logs.lines.join("\n")}</pre>
                <button type="button" onClick={loadMoreLogs} disabled={logsLoading}>
                  Load next 200
                </button>
              </>
            )}
          </div>
        )}
      </section>
    </main>
  );
}
