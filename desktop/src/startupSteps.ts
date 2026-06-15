/** Lifecycle states for a single startup step. */
export type StepStatus = "pending" | "running" | "ok" | "failed";

/** Runtime state of one startup step shown in the Screen 8 checklist. */
export interface StartupStepState {
  id: string;
  label: string;
  description: string;
  status: StepStatus;
  /** Non-null only when status === "failed". */
  error: string | null;
  /**
   * Route path to the setup screen that can repair this failure.
   * null means a simple Retry is the appropriate repair action.
   */
  repairScreen: string | null;
}

/** Canonical step IDs emitted by session.start $/progress notifications. */
export const STEP_CONFIG_MERGE = "config_merge";
export const STEP_CHECK_AGENT_AUTH = "check_agent_auth";
export const STEP_INSTALL_SKILLS = "install_skills";
export const STEP_INIT_BEADS = "init_beads";
export const STEP_BIND_IPC = "bind_ipc";
export const STEP_START_BRIDGE = "start_bridge";
export const STEP_FIRST_SNAPSHOT = "first_snapshot";

/** Ordered step IDs as they appear in the startup checklist. */
export const STARTUP_STEP_IDS = [
  STEP_CONFIG_MERGE,
  STEP_CHECK_AGENT_AUTH,
  STEP_INSTALL_SKILLS,
  STEP_INIT_BEADS,
  STEP_BIND_IPC,
  STEP_START_BRIDGE,
  STEP_FIRST_SNAPSHOT,
] as const;

interface StepMeta {
  label: string;
  description: string;
  /** Setup screen to navigate to for contextual repair, or null for Retry. */
  repairScreen: string | null;
}

/** Static metadata for each startup step. */
export const STEP_METADATA: Record<string, StepMeta> = {
  [STEP_CONFIG_MERGE]: {
    label: "Config merged",
    description: "agentshore.yaml updated from target branch, GitHub login, and agent choices.",
    repairScreen: "/setup/agents",
  },
  [STEP_CHECK_AGENT_AUTH]: {
    label: "Agent auth checked",
    description: "Each CLI agent's backend session (e.g. Codex login) is valid.",
    repairScreen: "/setup/agents",
  },
  [STEP_INSTALL_SKILLS]: {
    label: "Skills installed",
    description: "Project skill templates are current.",
    repairScreen: "/setup/agents",
  },
  [STEP_INIT_BEADS]: {
    label: "Beads ready",
    description: "Project graph is available and healthy.",
    repairScreen: "/setup/readiness",
  },
  [STEP_BIND_IPC]: {
    label: "IPC endpoint bound",
    description: "TCP loopback endpoint reserved.",
    repairScreen: null,
  },
  [STEP_START_BRIDGE]: {
    label: "Dashboard bridge starting",
    description: "Waiting for WebSocket readiness.",
    repairScreen: null,
  },
  [STEP_FIRST_SNAPSHOT]: {
    label: "First state snapshot",
    description: "Dashboard opens next.",
    repairScreen: null,
  },
};

/** Build the initial step list with all steps in "pending" state. */
export function buildInitialSteps(): StartupStepState[] {
  return STARTUP_STEP_IDS.map((id) => {
    const meta = STEP_METADATA[id];
    return {
      id,
      label: meta.label,
      description: meta.description,
      status: "pending",
      error: null,
      repairScreen: meta.repairScreen,
    };
  });
}

/**
 * Apply a progress notification to the current step list.
 * Returns a new array (pure — does not mutate the input).
 */
export function applyProgressEvent(
  steps: StartupStepState[],
  stepId: string,
  status: "running" | "ok" | "failed",
  error: string | null = null,
): StartupStepState[] {
  return steps.map((s) => {
    if (s.id !== stepId) return s;
    return { ...s, status, error: status === "failed" ? error : null };
  });
}
