import { useCallback, useEffect, useRef, useState, type JSX } from "react";

import "../dashboard.css";

import {
  BootstrapModal,
  notifyBootstrapModal,
} from "./BootstrapModal";
import { DashboardCanvas, notifyDashboardCanvasStickies } from "./DashboardCanvas";
import EventDrawer, {
  notifyEventDrawerEvent,
  notifyEventDrawerReset,
  notifyEventDrawerStateUpdate,
} from "./EventDrawer";
import { notifyEpicPanel } from "./EpicPanel";
import {
  FeedbackModal,
  notifyFeedbackModalHide,
  notifyFeedbackModalShow,
} from "./FeedbackModal";
import {
  notifyAgentPlayStatsEvent,
  notifyAgentPlayStatsReplay,
  notifyAgentPlayStatsReset,
} from "./AgentPlayStats";
import PlayBar, {
  notifyPlayBarActivePlay,
  notifyPlayBarClear,
  notifyPlayBarEvent,
  notifyPlayBarUpdate,
} from "./PlayBar";
import {
  PlaysPanelComponent,
  type DrainStatus,
  notifyPlaysPanelBudget,
  notifyPlaysPanelEvent,
  notifyPlaysPanelUpdate,
} from "./PlaysPanel";
import {
  SidePanelComponent,
  notifySidePanelActivePlay,
  notifySidePanelPlayEvent,
  notifySidePanelUpdate,
} from "./SidePanel";
import StageTabs, { type ViewMode } from "./StageTabs";
import KanbanStage, {
  notifyKanbanInsets,
  notifyKanbanStateUpdate,
  notifyKanbanVisible,
} from "./KanbanStage";
import StatsStage, {
  notifyStatsStageInsets,
  notifyStatsStageUpdate,
  notifyStatsStageVisible,
} from "./StatsStage";
import { ThemeToggle } from "./ThemeToggle";
import { TopBarHud, notifyTopBarHud } from "./TopBarHud";

import { dashboardLogger } from "../logger";
import { AgentShoreStateManager } from "../state";
import type { ResolvedTheme, ThemeMode } from "../theme";
import type { AgentShoreMessage, SessionState } from "../types";
import { WebSocketClient, type ConnectionState, type DashboardTransport } from "../ws";

/**
 * Single React surface that both the desktop app and the bridge SPA can
 * mount. Renders the bridge's HUD DOM structure (canvas + topbar +
 * main-area + bottom-bar + modals) so the existing CSS in dashboard.css
 * lays out the panels correctly without per-target overrides.
 *
 * Owns a AgentShoreStateManager and routes incoming transport messages to
 * the module-level notify\* functions exported by each component.
 *
 * Not yet ported (will be slotted in as those modules port to React):
 * - kanban + stats stage views (still imperative under dashboard/src/views/).
 * - The theme toggle in the top-right (desktop drives theme via
 *   data-theme on documentElement; the bridge SPA wires this in
 *   bootstrapDashboard.ts).
 */
export interface DashboardProps {
  /** WebSocket URL for the bridge state stream, e.g. ws://127.0.0.1:9999/ws. */
  wsUrl?: string;
  /** Test seam: supply a pre-built transport (e.g. demoTransport). */
  transport?: DashboardTransport;
  /** Initial theme until the bridge tells us otherwise. */
  theme?: ResolvedTheme;
  /**
   * Hide the in-HUD theme toggle. Defaults to showing the toggle, which
   * is right for both the bridge SPA and the desktop session route.
   */
  showThemeToggle?: boolean;
  /** Optional URL-driven theme mode for standalone dashboard QA links. */
  themeMode?: ThemeMode;
  /**
   * Fires once when the first ``instantiate_agent`` play_event arrives
   * with status="started". The desktop uses this to dismiss the
   * "Starting your session..." overlay — by the time the first agent
   * is dispatching, there's something happening on the floor and the
   * user no longer needs the modal.
   */
  onFirstAgentInstantiated?: () => void;
  /**
   * Fires once on the first ``state_update`` frame received from the
   * bridge. This is the "engine is confirmed live" signal: the bridge
   * is up and streaming state, even in a genuinely no-work session that
   * never spawns an agent. The desktop uses this to dismiss the
   * "Starting your session..." overlay so an idle session doesn't trap
   * the UI behind a permanent spinner (issue #10). ``onFirstAgentInstantiated``
   * remains a valid earlier fast-path; this is the always-arrives backstop.
   */
  onFirstStateUpdate?: () => void;
  /**
   * Fires on EVERY ``state_update`` frame that carries a ``session_state``,
   * with the latest session lifecycle phase. The desktop uses this to lock
   * the File > Adjust Budget control once the session is draining /
   * shutting_down — an absolute cap OVERRIDE silently no-ops past drain, so
   * the control is disabled rather than letting it fail silently (#244).
   */
  onSessionStateChange?: (state: SessionState) => void;
}

export function Dashboard({
  wsUrl,
  transport,
  theme = "light",
  showThemeToggle = true,
  themeMode,
  onFirstAgentInstantiated,
  onFirstStateUpdate,
  onSessionStateChange,
}: DashboardProps): JSX.Element {
  const [viewMode, setViewMode] = useState<ViewMode>("office");
  const stateManagerRef = useRef<AgentShoreStateManager | null>(null);
  if (stateManagerRef.current === null) {
    stateManagerRef.current = new AgentShoreStateManager();
  }
  const [connectionState, setConnectionState] = useState<ConnectionState>("closed");
  const transportRef = useRef<DashboardTransport | null>(null);
  const [drainState, setDrainState] = useState<DrainStatus>({
    visible: false,
    reason: null,
    connectionLost: false,
  });

  useEffect(() => {
    document.body.classList.add("dashboard-active");
    return () => {
      document.body.classList.remove("dashboard-active");
    };
  }, []);

  // Fire onFirstAgentInstantiated exactly once per Dashboard mount.
  // The ref resets when wsUrl changes (different session), so a
  // subsequent session also gets one shot at firing the callback.
  const firstAgentFiredRef = useRef(false);
  // Fire onFirstStateUpdate exactly once per Dashboard mount, on the
  // first state_update frame. Unlike the first-agent fast-path, a
  // state_update always arrives once the bridge is live — including in
  // no-work sessions that never spawn an agent (issue #10). Reset on
  // wsUrl change so a subsequent session also gets one shot.
  const firstStateUpdateFiredRef = useRef(false);
  useEffect(() => {
    firstAgentFiredRef.current = false;
    firstStateUpdateFiredRef.current = false;
  }, [wsUrl]);

  useEffect(() => {
    const stateManager = stateManagerRef.current;
    if (stateManager === null) return;
    if (transport === undefined && wsUrl === undefined) {
      return;
    }
    const client: DashboardTransport =
      transport ?? new WebSocketClient(wsUrl as string);
    transportRef.current = client;

    // Session-boundary reset (Tier 1): the manager detects a session_id change
    // on any frame and calls this hook to wipe accumulators that carry
    // per-session state but live outside the manager (play bar, agent stats,
    // event drawer, bootstrap modal). resetSession() already drops the
    // manager's own characters/seq/bootstrap; this clears the component DOM —
    // the modal only re-evaluates when notified, so pushing the cleared phase
    // hides a modal left up by the prior run before the new run re-shows it.
    stateManager.onSessionReset = () => {
      notifyPlayBarClear();
      notifyAgentPlayStatsReset();
      notifyEventDrawerReset();
      notifyBootstrapModal({ phase: null, startedAt: null });
    };

    client.onStateChange = (state) => {
      setConnectionState(state);
    };

    client.onMessage = (msg: AgentShoreMessage) => {
      stateManager.handleMessage(msg);
      switch (msg.type) {
        case "state_update":
          notifyTopBarHud(msg);
          notifySidePanelUpdate(msg);
          notifyEventDrawerStateUpdate(msg);
          notifyPlaysPanelUpdate(msg);
          notifyEpicPanel(msg);
          notifyPlayBarUpdate(msg);
          notifyStatsStageUpdate(msg);
          notifyKanbanStateUpdate(msg);
          notifyDashboardCanvasStickies(msg);
          // The engine is confirmed live the moment any state_update
          // arrives. Dismiss the desktop's session-starting overlay
          // here so a no-work session (which never instantiates an
          // agent) doesn't hang the spinner forever (issue #10).
          if (!firstStateUpdateFiredRef.current) {
            firstStateUpdateFiredRef.current = true;
            onFirstStateUpdate?.();
          }
          // Surface the session lifecycle phase on every frame so the desktop
          // can lock the Adjust Budget control once draining/shutting_down
          // (the absolute cap OVERRIDE silently no-ops past drain — #244).
          onSessionStateChange?.(msg.session_state);
          break;
        case "budget_update":
          // Budget-countdown heartbeat: refresh only the budget bar. Routed
          // nowhere near the office StateManager's agent handling, so the
          // sprites never re-process or jitter on these frequent frames.
          notifyPlaysPanelBudget(msg);
          break;
        case "play_event":
          notifyPlayBarEvent(msg);
          notifyAgentPlayStatsEvent(msg);
          notifyEventDrawerEvent(msg);
          notifySidePanelPlayEvent(msg);
          notifyPlaysPanelEvent(msg);
          // Dismiss the desktop's session-starting overlay on the
          // first instantiate_agent dispatch — by then the user is
          // looking at a populated office, not an empty floor.
          if (
            !firstAgentFiredRef.current &&
            msg.play_type === "instantiate_agent" &&
            msg.status === "started"
          ) {
            firstAgentFiredRef.current = true;
            onFirstAgentInstantiated?.();
          }
          break;
        case "active_play_replay":
          notifySidePanelActivePlay(msg.active_play);
          notifyPlayBarActivePlay(msg.active_play);
          break;
        case "feedback_requested":
          notifyFeedbackModalShow(msg.reason ?? "");
          break;
        case "bootstrap_phase":
          notifyBootstrapModal({
            phase: stateManager.bootstrapPhase,
            startedAt: stateManager.bootstrapStartedAt,
          });
          break;
        case "session_ended":
          notifyPlayBarClear();
          notifyFeedbackModalHide();
          notifyAgentPlayStatsReset();
          setDrainState({ visible: false, reason: null, connectionLost: false });
          break;
        case "session_draining":
          setDrainState({
            visible: true,
            reason: msg.reason ?? null,
            connectionLost: false,
          });
          break;
        case "session_paused":
          // Render the drain banner with a "PAUSED" label so the user
          // can see the orchestrator is paused (vs. ended/drained).
          setDrainState({
            visible: true,
            reason: `paused: ${msg.reason ?? "unknown"}`,
            connectionLost: false,
          });
          break;
        case "connection_lost":
          notifyPlayBarClear();
          setDrainState((prev) => ({ ...prev, connectionLost: prev.visible }));
          break;
        case "event_history_replay":
          // Replay broadcast on reconnect — feed the cached play events
          // into the agent-stats accumulator so per-agent counters
          // recover their history instead of starting from zero.
          notifyAgentPlayStatsReplay(msg.events ?? []);
          break;
        case "error":
          // Errors bypass the DEV/?debug=1 gate — production bugs were going
          // completely silent before. dashboardLogger.error handles surfacing.
          dashboardLogger.error("server", "received error frame", { msg });
          break;
        case "auth_token":
        case "read_only":
        case "agent_changed":
        case "connection_restored":
          // Already handled in transport (auth_token, read_only) or via
          // AgentShoreStateManager.handleMessage above; no extra notify.
          break;
        default:
          break;
      }
    };

    client.connect();
    return () => {
      client.disconnect();
      client.onMessage = null;
      client.onStateChange = null;
      if (transportRef.current === client) {
        transportRef.current = null;
      }
    };
  }, [transport, wsUrl]);

  const sendFeedbackCommand = useCallback((command: Record<string, unknown>) => {
    stateManagerRef.current?.clearFeedbackPending();
    notifyFeedbackModalHide();
    transportRef.current?.send(command);
  }, []);

  const connectionLabel =
    connectionState === "open"
      ? "live"
      : connectionState === "connecting"
      ? "connecting…"
      : connectionState === "reconnecting"
      ? "reconnecting…"
      : "offline";

  // Surface the connection target so a Tauri WebSocket policy block
  // (the most likely cause of a stuck "connecting…") is diagnosable
  // without opening devtools.
  const connectionTitle = wsUrl ? `WebSocket: ${wsUrl}` : "no WebSocket URL";

  // Mirror the imperative kanban-active / stats-active body classes the
  // existing CSS keys off of. Without these, the kanban + stats stages
  // wouldn't slide on top of the canvas.
  useEffect(() => {
    const body = document.body;
    body.classList.toggle("kanban-active", viewMode === "kanban");
    body.classList.toggle("stats-active", viewMode === "stats");
    notifyStatsStageVisible(viewMode === "stats");
    notifyKanbanVisible(viewMode === "kanban");
    return () => {
      body.classList.remove("kanban-active");
      body.classList.remove("stats-active");
    };
  }, [viewMode]);

  // Push insets to the kanban / stats stages so their columns slide
  // inside the surrounding HUD panels instead of overlapping them.
  // bootstrapDashboard.ts did the same via updateKanbanInsets /
  // updateStatsInsets — without it the kanban board paints under the
  // event drawer (left) and agents panel (right).
  useEffect(() => {
    if (viewMode === "office") return;

    const measureAndPush = () => {
      const topBar = document.getElementById("top-bar");
      const bottomBar = document.getElementById("bottom-bar");
      const leftPanel = document.getElementById("left-panel");
      const sidePanel = document.getElementById("side-panel");
      const winW = window.innerWidth;
      const winH = window.innerHeight;
      const topRect = topBar?.getBoundingClientRect();
      const bottomRect = bottomBar?.getBoundingClientRect();
      const leftRect = leftPanel?.getBoundingClientRect();
      const sideRect = sidePanel?.getBoundingClientRect();
      const top = topRect ? topRect.bottom : 0;
      const left =
        leftRect && !leftPanel?.classList.contains("collapsed") && leftRect.width > 0
          ? leftRect.right
          : 0;
      const right = sideRect && sideRect.width > 0 ? winW - sideRect.left : 0;
      const bottom = bottomRect ? winH - bottomRect.top : 0;
      if (viewMode === "kanban") {
        notifyKanbanInsets(top, left, right, bottom);
      } else if (viewMode === "stats") {
        notifyStatsStageInsets(top, left, right, bottom);
      }
    };

    measureAndPush();
    const raf = requestAnimationFrame(measureAndPush);
    window.addEventListener("resize", measureAndPush);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", measureAndPush);
    };
  }, [viewMode]);

  return (
    <>
      <DashboardCanvas
        theme={theme}
        stateManager={stateManagerRef.current}
        hidden={viewMode !== "office"}
      />

      <div id="kanban-stage" hidden={viewMode !== "kanban"}>
        <KanbanStage />
      </div>
      <StatsStage />

      <div id="hud">
        <div id="top-bar" className="dashboard-main-chrome">
          <div className="top-bar-left" id="topbar-left-mount">
            <TopBarHud />
          </div>
          <div id="stage-tabs" role="tablist" aria-label="Stage view">
            <StageTabs initial={viewMode} onChange={setViewMode} />
          </div>
          <div className="top-controls">
            {showThemeToggle && <ThemeToggle modeOverride={themeMode} />}
            <span id="connection-status" className="hud-chip" title={connectionTitle}>
              {connectionLabel}
            </span>
          </div>
        </div>

        <div id="main-area">
          <div id="left-rail">
            <div id="left-panel">
              <EventDrawer />
            </div>
          </div>
          <div id="side-panel">
            <SidePanelComponent />
          </div>
        </div>

        <div id="bottom-bar">
          <div id="plays-panel">
            <PlaysPanelComponent drainStatus={drainState} />
          </div>
          <PlayBar />
        </div>
      </div>

      <BootstrapModal />
      <FeedbackModal
        onContinue={() =>
          sendFeedbackCommand({
            command: "feedback_response",
            action: "continue",
          })
        }
        onPause={() =>
          sendFeedbackCommand({
            command: "feedback_response",
            action: "pause",
          })
        }
        onStop={() => sendFeedbackCommand({ command: "drain" })}
        onHardStop={() => sendFeedbackCommand({ command: "hard_stop" })}
        onAdjustBudget={(deltaUsd) =>
          sendFeedbackCommand({
            command: "adjust_budget",
            delta_usd: deltaUsd,
          })
        }
      />

    </>
  );
}
