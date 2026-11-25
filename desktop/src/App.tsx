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


        setOnboardingSeen(uiState.onboardingCompleted);
        setWelcomeOpen(!uiState.onboardingCompleted);





      } catch {
        setSetup(loadStoredSetup());
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [navigate, setTheme, setOnboardingSeen, setWelcomeOpen]);







  useEffect(() => {
    let cancelled = false;
    currentSession()
      .then((info) => {
        if (cancelled) return;


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



  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navigate, setDashboardUrl, setSessionReattaching, setWelcomeOpen]);

  useUiHeartbeat();
  useEsrEvents(navigate, setEsr);

  const onProjectSelected = async (_path: string) => {




    setLastProjectPath(_path);




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

    }
  };

  const onStartSession = (selection: StartSelection) => {




















    flushSync(() => {
      setSessionStarting(true);
    });
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        navigate("/starting", {
          state: {
            seedInputPath: selection.seedInputPath,
            ...(selection.timelapse !== undefined ? { timelapse: selection.timelapse } : {}),






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



            sessionReattaching ? null : (
              <ChooseProjectScreen
                onProjectSelected={onProjectSelected}
                onQuickStartFailed={(path, err, failedStep) => {




                  setQuickStartError({ message: err.message, step: failedStep });
                  navigate(`/setup/${failedStep}`);


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
