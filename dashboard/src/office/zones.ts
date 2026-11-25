import { ZoneId } from "./layout";


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
  "future_4",
  "take_break",
  "groom_backlog",
  "seed_project",
  "calibrate_alignment",
  "prune",
  "future_7",
  "future_8",
];



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
  "future_4",
  "prune",
  "run_qa",
  "take_break",
  "end_agent",
  "end_session",
  "future_7",
  "future_8",
];


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
  future_4: "Reserved 4",
  take_break: "Take Break",
  groom_backlog: "Groom Backlog",
  seed_project: "Seed Project",
  calibrate_alignment: "Calibrate",
  prune: "Prune",
  future_7: "Reserved 7",
  future_8: "Reserved 8",
};


export const PLAY_RESERVED: ReadonlySet<string> = new Set([
  "future_4",
  "future_7",
  "future_8",
]);



export const ZONE_ACCENTS: Record<ZoneId, string> = {
  [ZoneId.WAR_ROOM]: "#a78bfa",
  [ZoneId.WORKSHOP]: "#f0a030",
  [ZoneId.SCIENCE_LAB]: "#3dc878",
  [ZoneId.LAUNCH_CONTROL]: "#ff4040",
  [ZoneId.EDITORS_DESK]: "#ec6f55",
  [ZoneId.RECOVERY_BAY]: "#d95a6d",
  [ZoneId.ZEN_GARDEN]: "#9aa0a6",
  [ZoneId.FRONT_DESK]: "#9aa0a6",
};


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

  refine_task_breakdown: ZoneId.WAR_ROOM,
  seed_project: ZoneId.WAR_ROOM,
  groom_backlog: ZoneId.WAR_ROOM,
  calibrate_alignment: ZoneId.WAR_ROOM,


  issue_pickup: ZoneId.WORKSHOP,
  unblock_pr: ZoneId.WORKSHOP,
  systematic_debugging: ZoneId.WORKSHOP,
  cleanup: ZoneId.WORKSHOP,


  reconcile_state: ZoneId.RECOVERY_BAY,
  take_break: ZoneId.RECOVERY_BAY,
  prune: ZoneId.RECOVERY_BAY,


  run_qa: ZoneId.SCIENCE_LAB,


  merge_pr: ZoneId.LAUNCH_CONTROL,


  write_implementation_plan: ZoneId.EDITORS_DESK,
  code_review: ZoneId.EDITORS_DESK,
  design_audit: ZoneId.EDITORS_DESK,


  instantiate_agent: ZoneId.FRONT_DESK,


  end_agent: ZoneId.FRONT_DESK,
  end_session: ZoneId.FRONT_DESK,
};

export const FRONT_DESK_EXIT_PLAY_TYPES = new Set(["end_agent", "end_session"]);
export const CURRENT_LOCATION_PLAY_TYPES = new Set([
  "future_4",
  "future_7",
  "future_8",
]);
export const RECOVERY_PLAY_TYPES = new Set(["take_break"]);
