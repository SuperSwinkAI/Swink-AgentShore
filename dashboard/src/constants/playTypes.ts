/**
 * IN_PROGRESS_PLAYS — play types that represent active mutation of a repository
 * by an agent. When a play of this type is running on an issue, the kanban card
 * moves to the IN PROGRESS column.
 *
 * SYNC: must match mutation plays in src/agentshore/state.py PlayType enum.
 * Full enum (action-space v13):
 *   instantiate_agent, unblock_pr, write_implementation_plan, end_agent,
 *   issue_pickup, code_review, merge_pr, run_qa, systematic_debugging,
 *   design_audit, end_session, reconcile_state, refine_task_breakdown, cleanup,
 *   future_4, take_break, groom_backlog, seed_project,
 *   calibrate_alignment, prune, future_7, future_8
 *
 * Excluded from IN_PROGRESS_PLAYS (non-mutation / lifecycle plays):
 *   instantiate_agent — no issue context; agent creation
 *   end_agent         — lifecycle
 *   end_session       — lifecycle
 *   take_break        — pause
 *   design_audit      — project-level
 *   groom_backlog     — backlog-level, not per-issue
 *   seed_project      — project-level setup
 *   calibrate_alignment — project-level
 *   cleanup           — post-merge sweep (no open issue)
 *   reconcile_state   — self-heal of wedged state (no open issue)
 *   code_review       — drives REVIEWING column, not IN PROGRESS
 *   prune             — stale worktree/branch/beads sweep (no open issue)
 *   future_4/7/8      — reserved/masked slots
 */
export const IN_PROGRESS_PLAYS = new Set([
  "issue_pickup",
  "unblock_pr",
  "systematic_debugging",
  "merge_pr",
  "run_qa",
  "write_implementation_plan",
  "refine_task_breakdown",
]);
