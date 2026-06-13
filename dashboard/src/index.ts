export { AGENT_REGISTRY, AGENT_TYPES, agentLabel } from "./agentRegistry";
export type { AgentType, AgentRegistryEntry } from "./agentRegistry";
export { Dashboard } from "./components/Dashboard";
export type { DashboardProps } from "./components/Dashboard";
export { ErrorBoundary } from "./components/ErrorBoundary";
export { ThemeToggle } from "./components/ThemeToggle";
export type { ThemeToggleProps } from "./components/ThemeToggle";
export { DashboardCanvas } from "./components/DashboardCanvas";
export { HUD } from "./components/HUD";
export { PlaysPanel } from "./components/PlaysPanel";
export {
  PlaysPanelComponent,
  notifyPlaysPanelUpdate,
  notifyPlaysPanelEvent,
} from "./components/PlaysPanel";
export { IdentitiesScreen } from "./components/IdentitiesScreen";
export type {
  IdentitiesScreenProps,
  IdentitiesSidecar,
  IdentityRow,
  KeychainStatus,
} from "./components/IdentitiesScreen";
export { TrustedSourcesScreen } from "./components/TrustedSourcesScreen";
export type {
  TrustedSourcesScreenProps,
  TrustedSourcesSidecar,
} from "./components/TrustedSourcesScreen";
export {
  BootstrapModal,
  notifyBootstrapModal,
} from "./components/BootstrapModal";
export type { BootstrapModalState } from "./components/BootstrapModal";
export {
  FeedbackModal,
  notifyFeedbackModalShow,
  notifyFeedbackModalHide,
} from "./components/FeedbackModal";
export type { FeedbackModalProps } from "./components/FeedbackModal";
export { EpicPanel, notifyEpicPanel } from "./components/EpicPanel";
export {
  AgentPlayStats,
  useAgentPlayStats,
  getAgentPlayStats,
  notifyAgentPlayStatsEvent,
  notifyAgentPlayStatsReplay,
  notifyAgentPlayStatsReset,
} from "./components/AgentPlayStats";
export type {
  AgentStatsView,
  AgentPlayStatsProps,
} from "./components/AgentPlayStats";
export { default as EventDrawer } from "./components/EventDrawer";
export {
  notifyEventDrawerStateUpdate,
  notifyEventDrawerEvent,
  notifyEventDrawerReplay,
} from "./components/EventDrawer";
export {
  default as StageTabs,
  notifyStageTabsMode,
} from "./components/StageTabs";
export type { StageTabsProps, ViewMode } from "./components/StageTabs";
export { default as PlayBar } from "./components/PlayBar";
export {
  notifyPlayBarUpdate,
  notifyPlayBarEvent,
  notifyPlayBarActivePlay,
  notifyPlayBarClear,
} from "./components/PlayBar";
export { default as StatsStage } from "./components/StatsStage";
export {
  notifyStatsStageUpdate,
  notifyStatsStageVisible,
  notifyStatsStageInsets,
} from "./components/StatsStage";
export type { StatsStageInsets } from "./components/StatsStage";
export { default as KanbanStage } from "./components/KanbanStage";
export {
  notifyKanbanStateUpdate,
  notifyKanbanFocusedAgent,
  notifyKanbanVisible,
  notifyKanbanInsets,
} from "./components/KanbanStage";
export type { KanbanInternalState } from "./components/KanbanStage";

export { createDemoTransport, DemoTransport } from "./demoTransport";
export type { DemoScenario } from "./demoTransport";
