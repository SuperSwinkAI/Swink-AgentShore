/**
 * Unit-style tests for kanban board logic (phase.ts, constants/playTypes.ts).
 * Runs in the browser via Playwright page.evaluate so the ES module graph is
 * available through Vite's dev server — no separate unit test runner needed.
 */
import { expect, test } from "@playwright/test";

test("IN_PROGRESS_PLAYS is non-empty and contains known-correct members", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { IN_PROGRESS_PLAYS } = await import("/src/constants/playTypes.ts");
    return {
      size: IN_PROGRESS_PLAYS.size,
      hasIssuePickup: IN_PROGRESS_PLAYS.has("issue_pickup"),
      hasUnblockPr: IN_PROGRESS_PLAYS.has("unblock_pr"),
      hasSystematicDebugging: IN_PROGRESS_PLAYS.has("systematic_debugging"),
      hasRunQa: IN_PROGRESS_PLAYS.has("run_qa"),
      hasWriteImplementationPlan: IN_PROGRESS_PLAYS.has(
        "write_implementation_plan",
      ),
      // code_review and lifecycle plays must NOT be in IN_PROGRESS_PLAYS
      hasCodeReview: IN_PROGRESS_PLAYS.has("code_review"),
      hasEndAgent: IN_PROGRESS_PLAYS.has("end_agent"),
      hasEndSession: IN_PROGRESS_PLAYS.has("end_session"),
    };
  });

  expect(result.size).toBeGreaterThan(0);
  expect(result.hasIssuePickup).toBe(true);
  expect(result.hasUnblockPr).toBe(true);
  expect(result.hasSystematicDebugging).toBe(true);
  expect(result.hasRunQa).toBe(true);
  expect(result.hasWriteImplementationPlan).toBe(true);
  expect(result.hasCodeReview).toBe(false);
  expect(result.hasEndAgent).toBe(false);
  expect(result.hasEndSession).toBe(false);
});

test("M2: deriveColumns picks highest pr_number when multiple open PRs target the same issue", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { deriveColumns } = await import("/src/views/kanban/phase.ts");

    const makeIssue = (n: number) => ({
      issue_number: n,
      title: `Issue ${n}`,
      state: "open" as const,
      priority: null,
      labels: [],
      source: null,
      url: null,
      created_at: null,
      closed_at: null,
      bead_id: null,
      bead_epic_id: null,
      bead_epic_title: null,
      bead_status: null,
      bead_ready: false,
      bead_mirror_status: "missing" as const,
    });

    const makePr = (
      prNumber: number,
      issueNumber: number,
      state: "open" | "closed" | "merged" = "open",
    ) => ({
      pr_number: prNumber,
      title: `PR ${prNumber}`,
      state,
      branch: null,
      issue_number: issueNumber,
      labels: [],
      review_decision: null,
      status_check_summary: null,
      is_draft: false,
      blocked: false,
      blocked_reasons: [],
      url: null,
      github_author: null,
      author_agent_id: null,
      author_agent_type: null,
    });

    // Three open PRs targeting issue 42: 100 (old), 101 (mid), 102 (newest).
    // The kanban should pick PR 102.
    const issues = [makeIssue(42)];
    const prs = [makePr(100, 42), makePr(101, 42), makePr(102, 42)];
    const cols = deriveColumns(issues, [], prs, undefined);

    const reviewingCards = cols.reviewing;
    const card = reviewingCards.find((c) => c.issue?.issue_number === 42);
    return {
      columnCount: reviewingCards.length,
      prNumber: card?.pr?.pr_number ?? null,
    };
  });

  // Issue 42 should be in REVIEWING (open PR with no reviewer agent).
  expect(result.columnCount).toBeGreaterThanOrEqual(1);
  // Must pick the newest (highest pr_number) open PR.
  expect(result.prNumber).toBe(102);
});

test("M2: closed PR loses to any open PR regardless of pr_number order", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { deriveColumns } = await import("/src/views/kanban/phase.ts");

    const makeIssue = (n: number) => ({
      issue_number: n,
      title: `Issue ${n}`,
      state: "open" as const,
      priority: null,
      labels: [],
      source: null,
      url: null,
      created_at: null,
      closed_at: null,
      bead_id: null,
      bead_epic_id: null,
      bead_epic_title: null,
      bead_status: null,
      bead_ready: false,
      bead_mirror_status: "missing" as const,
    });

    const makePr = (
      prNumber: number,
      issueNumber: number,
      state: "open" | "closed" | "merged" = "open",
    ) => ({
      pr_number: prNumber,
      title: `PR ${prNumber}`,
      state,
      branch: null,
      issue_number: issueNumber,
      labels: [],
      review_decision: null,
      status_check_summary: null,
      is_draft: false,
      blocked: false,
      blocked_reasons: [],
      url: null,
      github_author: null,
      author_agent_id: null,
      author_agent_type: null,
    });

    // Old high-numbered closed PR + newer lower-numbered open PR.
    // The open PR should always win.
    const issues = [makeIssue(7)];
    const prs = [makePr(999, 7, "merged"), makePr(5, 7, "open")];
    const cols = deriveColumns(issues, [], prs, undefined);
    const card = [
      ...cols.reviewing,
      ...cols.in_progress,
      ...cols.todo,
      ...cols.done,
    ].find((c) => c.issue?.issue_number === 7);
    return {
      prNumber: card?.pr?.pr_number ?? null,
      prState: card?.pr?.state ?? null,
    };
  });

  expect(result.prNumber).toBe(5);
  expect(result.prState).toBe("open");
});

test("merged PRs render in Done even before issue-close mirroring", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { deriveColumns } = await import("/src/views/kanban/phase.ts");

    const issue = {
      issue_number: 42,
      title: "Issue 42",
      state: "open" as const,
      priority: null,
      labels: [],
      source: null,
      url: null,
      created_at: null,
      closed_at: null,
      bead_id: null,
      bead_epic_id: null,
      bead_epic_title: null,
      bead_status: null,
      bead_ready: false,
      bead_mirror_status: "missing" as const,
    };
    const makePr = (
      prNumber: number,
      issueNumber: number | null,
      title = `PR ${prNumber}`,
    ) => ({
      pr_number: prNumber,
      title,
      state: "merged" as const,
      branch: null,
      issue_number: issueNumber,
      labels: [],
      review_decision: null,
      status_check_summary: null,
      is_draft: false,
      blocked: false,
      blocked_reasons: [],
      url: null,
      github_author: null,
      author_agent_id: null,
      author_agent_type: null,
    });

    const cols = deriveColumns(
      [issue],
      [],
      [makePr(266, 42), makePr(267, null, "Orphan merged PR")],
      undefined,
    );

    return {
      doneIssuePr: cols.done.find((card) => card.issue?.issue_number === 42)
        ?.pr?.pr_number,
      orphanDonePr: cols.done.find((card) => card.pr?.pr_number === 267)?.pr
        ?.title,
      todoCount: cols.todo.length,
      reviewCount: cols.reviewing.length,
    };
  });

  expect(result.doneIssuePr).toBe(266);
  expect(result.orphanDonePr).toBe("Orphan merged PR");
  expect(result.todoCount).toBe(0);
  expect(result.reviewCount).toBe(0);
});

test("H7: deriveColumns returns cached result when hash unchanged", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { deriveColumns } = await import("/src/views/kanban/phase.ts");

    const makeIssue = (n: number) => ({
      issue_number: n,
      title: `Issue ${n}`,
      state: "open" as const,
      priority: null,
      labels: [],
      source: null,
      url: null,
      created_at: null,
      closed_at: null,
      bead_id: null,
      bead_epic_id: null,
      bead_epic_title: null,
      bead_status: null,
      bead_ready: false,
      bead_mirror_status: "missing" as const,
    });

    const issues = [makeIssue(1)];
    const first = deriveColumns(issues, [], [], undefined);
    // Call with a NEW array reference but identical content — must hit the cache.
    const second = deriveColumns([...issues], [], [], undefined);
    return { sameReference: first === second };
  });

  expect(result.sameReference).toBe(true);
});

test("H7: deriveColumns re-derives when issue state changes", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { deriveColumns } = await import("/src/views/kanban/phase.ts");

    const makeIssue = (
      n: number,
      state: "open" | "closed",
      closedAt: string | null = null,
    ) => ({
      issue_number: n,
      title: `Issue ${n}`,
      state,
      priority: null,
      labels: [],
      source: null,
      url: null,
      created_at: null,
      closed_at: closedAt,
      bead_id: null,
      bead_epic_id: null,
      bead_epic_title: null,
      bead_status: null,
      bead_ready: false,
      bead_mirror_status: "missing" as const,
    });

    const open = deriveColumns([makeIssue(1, "open")], [], [], undefined);
    const closed = deriveColumns(
      [makeIssue(1, "closed", new Date().toISOString())],
      [],
      [],
      undefined,
    );
    return {
      differentObjects: open !== closed,
      openColumn: open.todo.length === 1,
      doneColumn: closed.done.length === 1,
    };
  });

  expect(result.differentObjects).toBe(true);
  expect(result.openColumn).toBe(true);
  expect(result.doneColumn).toBe(true);
});

test("closed issue cards only render in Done for the last 24 hours", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { deriveColumns } = await import("/src/views/kanban/phase.ts");

    const hoursAgo = (hours: number) =>
      new Date(Date.now() - hours * 60 * 60 * 1000).toISOString();
    const makeIssue = (
      n: number,
      closedAt: string | null,
      title = `Issue ${n}`,
    ) => ({
      issue_number: n,
      title,
      state: "closed" as const,
      priority: null,
      labels: [],
      source: null,
      url: null,
      created_at: null,
      closed_at: closedAt,
      bead_id: null,
      bead_epic_id: null,
      bead_epic_title: null,
      bead_status: null,
      bead_ready: false,
      bead_mirror_status: "missing" as const,
    });

    const cols = deriveColumns(
      [
        makeIssue(1, hoursAgo(2), "recent closure"),
        makeIssue(2, hoursAgo(25), "old closure"),
        makeIssue(3, null, "missing timestamp"),
      ],
      [],
      [],
      undefined,
    );

    return {
      doneIssues: cols.done.map((card) => card.issue?.issue_number ?? null),
    };
  });

  expect(result.doneIssues).toEqual([1]);
});

test("closed bead-only tasks use closed_at recency, not updated_at", async ({
  page,
}) => {
  await page.goto("/?demo=1&scenario=empty&freeze=1");

  const result = await page.evaluate(async () => {
    const { deriveColumns } = await import("/src/views/kanban/phase.ts");

    const hoursAgo = (hours: number) =>
      new Date(Date.now() - hours * 60 * 60 * 1000).toISOString();
    const makeTask = (
      beadId: string,
      closedAt: string | null,
      updatedAt: string | null,
    ) => ({
      bead_id: beadId,
      title: beadId,
      status: "closed",
      parent_id: null,
      epic_id: null,
      epic_title: null,
      external_ref: null,
      issue_number: null,
      ready: false,
      closed_at: closedAt,
      updated_at: updatedAt,
    });

    const cols = deriveColumns(
      [],
      [],
      [],
      {
        epics: [],
        tasks: [
          makeTask("recent-closed", hoursAgo(1), hoursAgo(1)),
          makeTask("old-closed-recent-update", hoursAgo(30), hoursAgo(1)),
          makeTask("missing-closed-recent-update", null, hoursAgo(1)),
        ],
        tasks_ready: 0,
        tasks_total: 3,
        global_closure_ratio: 1,
      },
    );

    return {
      doneTasks: cols.done.map((card) => card.task?.bead_id ?? null),
    };
  });

  expect(result.doneTasks).toEqual(["recent-closed"]);
});
