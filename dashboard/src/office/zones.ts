import { ZoneId } from "./layout";

// Canonical V1 action-space order, parallel to Python's V1_ACTION_ORDER.
export const PLAY_KEYS: readonly string[] = [
  "instantiate_agent",
  "unblock_pr",
  "write_implementation_plan",
  "end_agent",
  "issue_pickup",
  "code_review",
  "merge_pr",
  "run_qa",
  "systematic_debugging",
  "design_audit",
  "end_session",
  "reconcile_state",
  "refine_task_breakdown",
  "cleanup",
  "browser_verification",
  "take_break",
  "groom_backlog",
  "seed_project",
  "calibrate_alignment",
  "prune",
  "future_7",
  "future_8",
];

// Display order for the bottom plays tray. The tray grid flows by column, so
// each adjacent pair represents top/bottom cells for one visual column.
export const PLAY_TRAY_KEYS: readonly string[] = [
  "instantiate_agent",
  "seed_project",
  "design_audit",
  "groom_backlog",
  "refine_task_breakdown",
  "calibrate_alignment",
  "write_implementation_plan",
  "cleanup",
  "issue_pickup",
  "systematic_debugging",
  "code_review",
  "unblock_pr",
  "merge_pr",
  "reconcile_state",
  "browser_verification",
  "prune",
  "run_qa",
  "take_break",
  "end_agent",
  "end_session",
  "future_7",
  "future_8",
];

// Human-readable names for each play key (sourced from design handoff plays-data.jsx).
export const PLAY_DISPLAY_NAMES: Record<string, string> = {
  instantiate_agent: "New Agent",
  unblock_pr: "Unblock PR",
  write_implementation_plan: "Write Plan",
  end_agent: "End Agent",
  issue_pickup: "Issue Pickup",
  code_review: "Code Review",
  merge_pr: "Merge PR",
  run_qa: "Run QA",
  systematic_debugging: "Debug",
  design_audit: "Design Audit",
  end_session: "End Session",
  reconcile_state: "Reconcile",
  refine_task_breakdown: "Refine Tasks",
  cleanup: "Clean Up",
  browser_verification: "Browser Verify",
  take_break: "Take Break",
  groom_backlog: "Groom Backlog",
  seed_project: "Seed Project",
  calibrate_alignment: "Calibrate",
  prune: "Prune",
  future_7: "Reserved 7",
  future_8: "Reserved 8",
};

// Reserved action slots (italicized in the Plays Panel, always masked by the policy).
export const PLAY_RESERVED: ReadonlySet<string> = new Set([
  "future_7",
  "future_8",
]);

// Zone accent colors used by the Plays Panel cards.
// violet and oxblood are panel-local additions; others match --color-fm-* tokens.
export const ZONE_ACCENTS: Record<ZoneId, string> = {
  [ZoneId.WAR_ROOM]: "#a78bfa", // violet — planning
  [ZoneId.WORKSHOP]: "#f0a030", // amber — matches --color-fm-busy
  [ZoneId.SCIENCE_LAB]: "#3dc878", // green — matches --color-fm-ok
  [ZoneId.LAUNCH_CONTROL]: "#ff4040", // red — matches --color-fm-hot
  [ZoneId.EDITORS_DESK]: "#ec6f55", // oxblood — review
  [ZoneId.RECOVERY_BAY]: "#d95a6d", // red — failures and cooldowns
  [ZoneId.ZEN_GARDEN]: "#9aa0a6", // mute — matches --color-fm-mute
  [ZoneId.FRONT_DESK]: "#9aa0a6", // mute — lifecycle/structural
};

// Short labels used in Plays Panel tooltips.
export const ZONE_LABELS: Record<ZoneId, string> = {
  [ZoneId.WAR_ROOM]: "War Room",
  [ZoneId.WORKSHOP]: "Workshop",
  [ZoneId.SCIENCE_LAB]: "Science Lab",
  [ZoneId.LAUNCH_CONTROL]: "Launch Control",
  [ZoneId.EDITORS_DESK]: "Editor's Desk",
  [ZoneId.RECOVERY_BAY]: "Recovery Bay",
  [ZoneId.ZEN_GARDEN]: "Zen Garden",
  [ZoneId.FRONT_DESK]: "Front Desk",
};

export const PLAY_TO_ZONE: Record<string, ZoneId> = {
  // War Room — planning
  refine_task_breakdown: ZoneId.WAR_ROOM,
  seed_project: ZoneId.WAR_ROOM,
  groom_backlog: ZoneId.WAR_ROOM,
  calibrate_alignment: ZoneId.WAR_ROOM,

  // Workshop — code work
  issue_pickup: ZoneId.WORKSHOP,
  unblock_pr: ZoneId.WORKSHOP,
  systematic_debugging: ZoneId.WORKSHOP,
  cleanup: ZoneId.WORKSHOP,

  // Recovery Bay — self-heal of wedged state, cooldowns, error recovery
  reconcile_state: ZoneId.RECOVERY_BAY,
  take_break: ZoneId.RECOVERY_BAY,
  prune: ZoneId.RECOVERY_BAY,

  // Science Lab — QA & verification
  run_qa: ZoneId.SCIENCE_LAB,
  browser_verification: ZoneId.SCIENCE_LAB,

  // Launch Control — deploy & merge
  merge_pr: ZoneId.LAUNCH_CONTROL,

  // Editor's Desk — critique and implementation plans
  write_implementation_plan: ZoneId.EDITORS_DESK,
  code_review: ZoneId.EDITORS_DESK,
  design_audit: ZoneId.EDITORS_DESK,

  // Front Desk — agent arrival
  instantiate_agent: ZoneId.FRONT_DESK,

  // Agent lifecycle — routed specially by the state manager.
  end_agent: ZoneId.FRONT_DESK,
  end_session: ZoneId.FRONT_DESK,
};

export const FRONT_DESK_EXIT_PLAY_TYPES = new Set(["end_agent", "end_session"]);
export const CURRENT_LOCATION_PLAY_TYPES = new Set([
  "future_7",
  "future_8",
]);
export const RECOVERY_PLAY_TYPES = new Set(["take_break"]);
