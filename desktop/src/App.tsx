import { invoke } from "@tauri-apps/api/core";
import logoUrl from "./assets/brand/logo.svg";
import { useContext, useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { DashboardCanvas } from "@agentshore/dashboard";
import { budgetHydrationToSelection, inspectProject } from "./rpc/projectClient";
import { SessionContext } from "./services/sessionContext";
import { currentSession } from "./rpc/sessionClient";
import { DemoDashboardScreen } from "./screens/DemoDashboardScreen";
import { SessionDashboardScreen } from "./screens/SessionDashboardScreen";
import { SessionStartingOverlay } from "./SessionStartingOverlay";
import { EndSessionReportScreen } from "./screens/EndSessionReportScreen";

import { AgentConfigScreen } from "./screens/AgentConfigScreen";
import { ChooseProjectScreen } from "./screens/ChooseProjectScreen";
import { FatalErrorScreen } from "./screens/FatalErrorScreen";
import { RecoveryScreen } from "./screens/RecoveryScreen";
import { type StartSelection } from "./screens/StartScreen";
import { TargetBranchScreen } from "./screens/TargetBranchScreen";
import { StartingProgressRoute } from "./StartingProgressRoute";
import { AppMenu } from "./components/AppMenu";
import { WelcomeCarousel } from "./components/WelcomeCarousel";
import { SchemaDriftBanner } from "./components/SchemaDriftBanner";
import { SetupLayout } from "./setup/SetupLayout";
import {
  defaultSetupState,
  loadStoredSetup,
  persistSetup,
  type SetupScreen,
  type SetupState,
} from "./setup/setupState";
import { normalizeTheme, useThemeSync } from "./theme";
import { useUiHeartbeat } from "./hooks/useUiHeartbeat";
import { useWelcomeOnboarding } from "./hooks/useWelcomeOnboarding";
import { useEsrEvents } from "./hooks/useEsrEvents";
import { useSchemaDriftWarning } from "./hooks/useSchemaDriftWarning";
import { useSidecarCrashListener } from "./hooks/useSidecarCrashListener";
import { useFatalErrorListener } from "./hooks/useFatalErrorListener";
import { useDashboardBodyClass, useDemoDashboardShortcut } from "./hooks/useShellRouting";

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

export function App() {
  const { theme, setTheme, onThemeChange } = useThemeSync();
  const [setup, setSetup] = useState<SetupState>(defaultSetupState);
  const [quickStartError, setQuickStartError] = useState<
    { message: string; step: SetupScreen } | null
  >(null);
  const {
    welcomeOpen,
    setWelcomeOpen,
    onboardingSeen,
    setOnboardingSeen,
    markWelcomeSeen,
    setWelcomeSeen,
  } = useWelcomeOnboarding();
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
  const crashPayload = useSidecarCrashListener(navigate);
  const fatalInfo = useFatalErrorListener(navigate);
  const [schemaDrift, setSchemaDrift] = useSchemaDriftWarning();

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
  }, [navigate, setTheme, setOnboardingSeen, setWelcomeOpen]);

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

  useUiHeartbeat();
  useEsrEvents(navigate, setEsr);

  const onProjectSelected = async (_path: string) => {
    // Track the most-recent project path on the session context so the
    // app menu's re-open-project affordance has it without re-querying
    // project.inspect. Set unconditionally — even if the hydration step
    // below fails, the path is still useful.
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

  useDashboardBodyClass(location.pathname);
  useDemoDashboardShortcut(navigate);

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
      <SchemaDriftBanner
        warning={schemaDrift}
        onDismiss={() => setSchemaDrift(null)}
        onDesignated={() => setSchemaDrift(null)}
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
