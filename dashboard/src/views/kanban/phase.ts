import type {
  AgentSnapshot,
  EpicStatus,
  GraphTask,
  IssueSnapshot,
  ProjectGraph,
  PullRequestSnapshot,
} from "../../types";
import { IN_PROGRESS_PLAYS } from "../../constants/playTypes";

export type Phase = "todo" | "in_progress" | "reviewing" | "done";

export const PHASES: Phase[] = ["todo", "in_progress", "reviewing", "done"];

export const PHASE_LABEL: Record<Phase, string> = {
  todo: "TO DO",
  in_progress: "IN PROGRESS",
  reviewing: "IN REVIEW",
  done: "DONE",
};

export interface KanbanCard {
  // Null for orphan-PR cards (PR has no issue link, or its issue is not in
  // the open-issues list). Renderer falls back to the PR's own metadata.
  issue: IssueSnapshot | null;
  task: GraphTask | null;
  epic: EpicStatus | null;
  pr: PullRequestSnapshot | null;
  authorAgent: AgentSnapshot | null;
  reviewerAgent: AgentSnapshot | null;
  pulse: boolean;
  blocked: boolean;
  rejected: boolean;
}

export interface KanbanColumns {
  todo: KanbanCard[];
  in_progress: KanbanCard[];
  reviewing: KanbanCard[];
  done: KanbanCard[];
}

function findActiveAgentForIssue(
  agents: AgentSnapshot[],
  issueNumber: number,
): AgentSnapshot | null {
  for (const agent of agents) {
    if (agent.current_play?.issue_number !== issueNumber) continue;
    const play = agent.current_play?.play_type ?? "";
    if (IN_PROGRESS_PLAYS.has(play)) return agent;
  }
  return null;
}

function findReviewerForPr(
  agents: AgentSnapshot[],
  prNumber: number | null,
): AgentSnapshot | null {
  if (prNumber === null) return null;
  for (const agent of agents) {
    if (
      agent.current_play?.play_type === "code_review" &&
      agent.current_play?.pr_number === prNumber
    ) {
      return agent;
    }
  }
  return null;
}

function findAuthorAgent(
  agents: AgentSnapshot[],
  pr: PullRequestSnapshot | null,
): AgentSnapshot | null {
  if (!pr || !pr.author_agent_id) return null;
  return agents.find((a) => a.agent_id === pr.author_agent_id) ?? null;
}

/**
 * Compute a lightweight content hash for memo comparison.
 * Re-renders when any issue's number or state changes; any agent's id, status,
 * or current-play changes; any PR's number, state, review decision, or blocked
 * flag changes; or graph summary counters change.
 *
 * This is intentionally a string-concatenation hash rather than reference
 * equality so the memo stays correct even if the bridge reuses array
 * references for optimization.
 */
function _contentHash(
  issues: IssueSnapshot[],
  agents: AgentSnapshot[],
  prs: PullRequestSnapshot[],
  graph: ProjectGraph | null | undefined,
): string {
  const issueKey = issues
    .map(
      (i) =>
        `${i.issue_number}:${i.state}:${i.title}:${i.labels.join("+")}:${i.url ?? ""}:${i.created_at ?? ""}:${i.closed_at ?? ""}:${i.bead_mirror_status}:${i.bead_status ?? ""}:${i.bead_ready}`,
    )
    .join(",");
  const agentKey = agents
    .map(
      (a) =>
        `${a.agent_id}:${a.status}:${a.current_play?.play_type ?? ""}:${a.current_play?.issue_number ?? ""}:${a.current_play?.pr_number ?? ""}`,
    )
    .join(",");
  const prKey = prs
    .map(
      (p) =>
        `${p.pr_number}:${p.title}:${p.state}:${p.review_decision ?? ""}:${p.blocked}:${p.url ?? ""}:${p.labels.join("+")}`,
    )
    .join(",");
  const graphKey = graph
    ? `${graph.tasks_total}:${graph.tasks_ready}:${graph.global_closure_ratio}`
    : "null";
  return `${issueKey}|${agentKey}|${prKey}|${graphKey}`;
}

let _lastHash: string | null = null;
let _lastOutput: KanbanColumns | null = null;

export function deriveColumns(
  issues: IssueSnapshot[],
  agents: AgentSnapshot[],
  prs: PullRequestSnapshot[],
  graph: ProjectGraph | null | undefined,
): KanbanColumns {
  const now = Date.now();
  const hash = `${_contentHash(issues, agents, prs, graph)}|${Math.floor(
    now / RECENCY_CACHE_BUCKET_MS,
  )}`;
  if (_lastOutput !== null && _lastHash === hash) {
    return _lastOutput;
  }
  _lastHash = hash;
  _lastOutput = _deriveColumns(issues, agents, prs, graph, now);
  return _lastOutput;
}

const TWENTY_FOUR_HOURS_MS = 24 * 60 * 60 * 1000;
const RECENCY_CACHE_BUCKET_MS = 60 * 1000;

function isRecentlyClosed(closedAt: string | null | undefined, now: number): boolean {
  if (!closedAt) return false;
  const closedTime = new Date(closedAt).getTime();
  if (Number.isNaN(closedTime)) return false;
  return now - closedTime <= TWENTY_FOUR_HOURS_MS;
}

function _deriveColumns(
  issues: IssueSnapshot[],
  agents: AgentSnapshot[],
  prs: PullRequestSnapshot[],
  graph: ProjectGraph | null | undefined,
  now: number,
): KanbanColumns {
  const epicById = new Map<string, EpicStatus>(
    (graph?.epics ?? []).map((epic) => [epic.bead_id, epic]),
  );
  const taskByIssue = new Map<number, GraphTask>();
  for (const task of graph?.tasks ?? []) {
    if (task.issue_number !== null) {
      taskByIssue.set(task.issue_number, task);
    }
  }

  const prByIssue = new Map<number, PullRequestSnapshot>();
  for (const pr of prs) {
    if (pr.issue_number !== null) {
      const existing = prByIssue.get(pr.issue_number);
      // Prefer open PRs over closed ones for column placement.
      // When multiple open PRs target the same issue (e.g. after unblock_pr
      // force-pushes and opens a replacement), pick the most recently created
      // one (highest pr_number) so stale PRs do not win by accident.
      if (!existing) {
        prByIssue.set(pr.issue_number, pr);
      } else if (existing.state !== "open" && pr.state === "open") {
        // Always prefer an open PR over a closed/merged one.
        prByIssue.set(pr.issue_number, pr);
      } else if (
        existing.state === "open" &&
        pr.state === "open" &&
        pr.pr_number > existing.pr_number
      ) {
        // Among open PRs, take the highest pr_number (most recently created).
        prByIssue.set(pr.issue_number, pr);
      }
    }
  }

  const cols: KanbanColumns = {
    todo: [],
    in_progress: [],
    reviewing: [],
    done: [],
  };

  for (const issue of issues) {
    if (issue.state === "closed" && !isRecentlyClosed(issue.closed_at, now)) {
      continue;
    }

    const task = taskByIssue.get(issue.issue_number) ?? null;
    const epicId = issue.bead_epic_id ?? task?.epic_id ?? null;
    const epic = epicId ? (epicById.get(epicId) ?? null) : null;
    const pr = prByIssue.get(issue.issue_number) ?? null;
    const reviewer = findReviewerForPr(agents, pr?.pr_number ?? null);
    const activeAgent = findActiveAgentForIssue(agents, issue.issue_number);
    const author = findAuthorAgent(agents, pr);

    let phase: Phase;
    if (issue.state === "closed" || pr?.state === "merged") {
      phase = "done";
    } else if (reviewer !== null) {
      phase = "reviewing";
    } else if (activeAgent !== null) {
      phase = "in_progress";
    } else if (pr && pr.state === "open") {
      // PR exists but no review running — keep visible in REVIEWING so the
      // board does not lose track of in-flight work.
      phase = "reviewing";
    } else {
      phase = "todo";
    }

    const authorAgent: AgentSnapshot | null =
      phase === "in_progress" ? activeAgent : (author ?? activeAgent);

    const blocked = !!pr?.blocked;
    const rejected = pr?.review_decision === "CHANGES_REQUESTED";
    const pulse =
      (activeAgent !== null && activeAgent.status === "busy") ||
      (reviewer !== null && reviewer.status === "busy");

    cols[phase].push({
      issue,
      task,
      epic,
      pr,
      authorAgent,
      reviewerAgent: reviewer,
      pulse,
      blocked,
      rejected,
    });
  }

  // Orphan PRs: open PRs whose linked issue is not in the issues list, or
  // which have no issue_number at all. Surface them as standalone cards in
  // REVIEWING so operators see every open PR even when the kanban cannot
  // anchor it to an issue card.
  const issueNumbers = new Set(issues.map((i) => i.issue_number));
  const orphanReviewPrs = prs
    .filter(
      (pr) =>
        pr.state === "open" &&
        (pr.issue_number === null || !issueNumbers.has(pr.issue_number)),
    )
    .sort((a, b) => b.pr_number - a.pr_number);

  for (const pr of orphanReviewPrs) {
    const reviewer = findReviewerForPr(agents, pr.pr_number);
    const author = findAuthorAgent(agents, pr);
    const blocked = !!pr.blocked;
    const rejected = pr.review_decision === "CHANGES_REQUESTED";
    const pulse = reviewer !== null && reviewer.status === "busy";
    cols.reviewing.push({
      issue: null,
      task: null,
      epic: null,
      pr,
      authorAgent: author,
      reviewerAgent: reviewer,
      pulse,
      blocked,
      rejected,
    });
  }

  const orphanMergedPrs = prs
    .filter(
      (pr) =>
        pr.state === "merged" &&
        (pr.issue_number === null || !issueNumbers.has(pr.issue_number)),
    )
    .sort((a, b) => b.pr_number - a.pr_number);

  for (const pr of orphanMergedPrs) {
    const author = findAuthorAgent(agents, pr);
    cols.done.push({
      issue: null,
      task: null,
      epic: null,
      pr,
      authorAgent: author,
      reviewerAgent: null,
      pulse: false,
      blocked: false,
      rejected: false,
    });
  }

  const beadOnlyTasks = (graph?.tasks ?? [])
    .filter(
      (task) =>
        task.issue_number === null || !issueNumbers.has(task.issue_number),
    )
    .sort((a, b) => a.bead_id.localeCompare(b.bead_id));

  for (const task of beadOnlyTasks) {
    if (task.status === "closed") {
      // Avoid accumulating ancient closed beads-only tasks in DONE forever.
      // Use the actual close time only; a recent metadata update should not
      // make old completed work look newly relevant.
      if (!isRecentlyClosed(task.closed_at, now)) continue;
    }

    const epic = task.epic_id ? (epicById.get(task.epic_id) ?? null) : null;
    cols[task.status === "closed" ? "done" : "todo"].push({
      issue: null,
      task,
      epic,
      pr: null,
      authorAgent: null,
      reviewerAgent: null,
      pulse: false,
      blocked: task.status === "blocked",
      rejected: false,
    });
  }

  return cols;
}
