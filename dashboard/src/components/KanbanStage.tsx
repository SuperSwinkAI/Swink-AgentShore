import React, { useState } from "react";
import { createPortal } from "react-dom";
import type {
  AgentSnapshot,
  PullRequestSnapshot,
  ProjectGraph,
  StateUpdate,
} from "../types";
import {
  deriveColumns,
  PHASES,
  PHASE_LABEL,
  type KanbanCard,
  type KanbanColumns,
  type Phase,
} from "../views/kanban/phase";
import { createNotifyStore } from "../notifyStore";

// Constants mirror render.ts.

const EPIC_HUES: number[] = [
  232, 38, 286, 210, 195, 0, 100, 220, 142, 322, 30, 260,
];
const FALLBACK_HUE = 50;

const PHASE_BULLET_COLOR: Record<Phase, string> = {
  todo: "var(--color-fm-idle)",
  in_progress: "var(--color-fm-busy)",
  reviewing: "var(--color-fm-idle)",
  done: "var(--color-fm-ok)",
};

// Helpers mirror render.ts/detailModal.ts.

function hueForEpic(
  epicId: string | null | undefined,
  epicIds: string[],
): number {
  if (!epicId) return FALLBACK_HUE;
  const index = epicIds.indexOf(epicId);
  return index >= 0 ? EPIC_HUES[index % EPIC_HUES.length] : FALLBACK_HUE;
}

function agentInitials(agentId: string): string {
  const parts = agentId.split(/[-_]/);
  if (parts.length >= 2) {
    const a = parts[0][0] ?? "";
    const b = parts[1][0] ?? "";
    return (a + b).toUpperCase();
  }
  return agentId.slice(0, 2).toUpperCase();
}

function compactTags(
  card: KanbanCard,
  mirrorStatus: string,
  readyText: string,
  agentId: string | null,
): string {
  const tags: string[] = [];
  if (card.issue?.labels.length) tags.push(...card.issue.labels);
  if (card.pr) tags.push(`PR #${card.pr.pr_number}`);
  if (card.pr?.blocked) tags.push("blocked");
  if (card.pr?.review_decision === "CHANGES_REQUESTED")
    tags.push("changes requested");
  if (mirrorStatus) tags.push(mirrorStatus);
  if (readyText) tags.push(readyText);
  if (card.epic?.title) tags.push(card.epic.title);
  if (agentId) tags.push(agentInitials(agentId));
  return [...new Set(tags.filter(Boolean))].join(" | ");
}

function phaseLabel(phase: Phase): string {
  return phase.replace("_", " ").toUpperCase();
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "None";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function agentSummary(agent: AgentSnapshot): string {
  return `${agent.display_name || agent.agent_id} (${agent.status})`;
}

function titleCase(value: string | null | undefined, empty = "Unknown"): string {
  if (!value) return empty;
  return value
    .replace(/[_-]+/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function subtitleFor(card: KanbanCard, phase: Phase): string {
  if (card.issue)
    return `Issue #${card.issue.issue_number} | ${phaseLabel(phase)}`;
  if (card.pr)
    return `Pull request #${card.pr.pr_number} | ${phaseLabel(phase)}`;
  if (card.task) return `${card.task.bead_id} | ${phaseLabel(phase)}`;
  return phaseLabel(phase);
}

function epicTitleFor(card: KanbanCard): string {
  return (
    card.epic?.title ??
    card.issue?.bead_epic_title ??
    card.task?.epic_title ??
    "Unlinked"
  );
}

function beadIdFor(card: KanbanCard): string {
  return card.issue?.bead_id ?? card.task?.bead_id ?? "None";
}

function beadStatusFor(card: KanbanCard): string {
  return card.issue?.bead_status ?? card.task?.status ?? "Unknown";
}

function mirrorStatusFor(card: KanbanCard): string {
  return card.issue?.bead_mirror_status ?? (card.task ? "unlinked" : "missing");
}

function isReady(card: KanbanCard): boolean {
  return Boolean(card.issue?.bead_ready || card.task?.ready);
}

function reviewText(pr: PullRequestSnapshot | null): string {
  return pr ? titleCase(pr.review_decision, "No review") : "No linked PR";
}

function checksText(pr: PullRequestSnapshot | null): string {
  return pr ? titleCase(pr.status_check_summary, "Checks unknown") : "No linked PR";
}

function percentText(value: number | null | undefined): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return null;
  }
  const clamped = Math.min(100, Math.max(0, Math.round(value * 100)));
  return `${clamped}%`;
}

function splitTags(tags: string): string[] {
  return [...new Set(tags.split(" | ").map((tag) => tag.trim()).filter(Boolean))];
}

function phaseTone(phase: Phase): string {
  if (phase === "done") return "good";
  if (phase === "in_progress") return "warn";
  if (phase === "reviewing") return "info";
  return "neutral";
}

function reviewTone(pr: PullRequestSnapshot | null): string {
  if (!pr?.review_decision) return "neutral";
  if (pr.review_decision === "APPROVED") return "good";
  if (pr.review_decision === "CHANGES_REQUESTED") return "danger";
  return "info";
}

function checksTone(pr: PullRequestSnapshot | null): string {
  if (!pr?.status_check_summary) return "warn";
  const normalized = pr.status_check_summary.toLowerCase();
  if (["success", "passed", "passing"].includes(normalized)) return "good";
  if (["failed", "failure", "error"].includes(normalized)) return "danger";
  return "warn";
}

function summaryHeadline(card: KanbanCard): string {
  const issueState = card.issue
    ? `Issue ${titleCase(card.issue.state)}`
    : card.pr
      ? "Pull Request Card"
      : "Beads Task";
  const beadState =
    beadIdFor(card) === "None"
      ? "no linked bead"
      : `bead ${beadIdFor(card)} ${titleCase(beadStatusFor(card)).toLowerCase()}`;
  const prState = card.pr
    ? `PR #${card.pr.pr_number} ${titleCase(card.pr.state).toLowerCase()}`
    : "no linked PR";
  return `${issueState} - ${beadState} - ${prState}`;
}

function footerSummary(card: KanbanCard): string {
  const issueState = card.issue ? `issue ${card.issue.state}` : "no issue";
  const beadReady = isReady(card) ? "bead ready" : "bead not ready";
  const prReview = card.pr
    ? `PR ${reviewText(card.pr).toLowerCase()}`
    : "no linked PR";
  const checks = card.pr ? checksText(card.pr).toLowerCase() : "checks unavailable";
  return `Last known state: ${issueState}, ${beadReady}, ${prReview}, ${checks}`;
}

function copyText(value: string | number | null | undefined): void {
  if (value === null || value === undefined || value === "") return;
  if (!navigator.clipboard) return;
  void navigator.clipboard.writeText(String(value)).catch(() => undefined);
}

interface KanbanInsets {
  top: number;
  left: number;
  right: number;
  bottom: number;
}

interface KanbanInternalState {
  state: StateUpdate | null;
  focusedAgent: string | null;
  visible: boolean;
  insets: KanbanInsets | null;
}

const store = createNotifyStore<KanbanInternalState>({
  state: null,
  focusedAgent: null,
  visible: true,
  insets: null,
});

export function notifyKanbanStateUpdate(state: StateUpdate): void {
  store.notify({ ...store.get(), state });
}

export function notifyKanbanFocusedAgent(agentId: string | null): void {
  store.notify({ ...store.get(), focusedAgent: agentId });
}

export function notifyKanbanVisible(visible: boolean): void {
  store.notify({ ...store.get(), visible });
}

export function notifyKanbanInsets(
  top: number,
  left: number,
  right: number,
  bottom: number,
): void {
  store.notify({ ...store.get(), insets: { top, left, right, bottom } });
}

interface ResolvedCard {
  cardId: string;
  card: KanbanCard;
  phase: Phase;
  hue: number;
  agentIds: string[];
  isFocused: boolean;
  badgeAgent: AgentSnapshot | null;
  titleText: string;
  titlePrefix: string;
  tags: string;
  issueNumber: number | "";
  prNumber: number | "";
}

function resolveCardsForPhase(
  cards: KanbanCard[],
  phase: Phase,
  epicIds: string[],
  focusedAgent: string | null,
): ResolvedCard[] {
  return cards.map((card) => {
    const hue = hueForEpic(card.epic?.bead_id, epicIds);
    const agentIds: string[] = [];
    if (card.authorAgent) agentIds.push(card.authorAgent.agent_id);
    if (card.reviewerAgent) agentIds.push(card.reviewerAgent.agent_id);
    const isFocused =
      focusedAgent !== null && agentIds.some((id) => id === focusedAgent);
    const badgeAgent = card.authorAgent ?? card.reviewerAgent;
    const issueNumber = card.issue?.issue_number ?? "";
    const prNumber = card.pr?.pr_number ?? "";
    const titleText = card.issue
      ? card.issue.title
      : card.pr
        ? `PR #${card.pr.pr_number} — ${card.pr.title}`
        : card.task
          ? card.task.title
          : "(unknown)";
    const titlePrefix = card.issue
      ? `#${card.issue.issue_number}`
      : card.pr
        ? `PR #${card.pr.pr_number}`
        : card.task
          ? card.task.bead_id
          : "card";
    const mirrorStatus =
      card.issue?.bead_mirror_status ?? (card.task ? "unlinked" : "");
    const statusText = card.issue?.bead_status ?? card.task?.status ?? "";
    const readyText =
      card.issue?.bead_ready || card.task?.ready ? "ready" : statusText;
    const tags = compactTags(
      card,
      mirrorStatus,
      readyText,
      badgeAgent?.agent_id ?? null,
    );
    const cardId = `${phase}-${card.issue?.issue_number ?? ""}-${card.pr?.pr_number ?? ""}-${card.task?.bead_id ?? ""}`;
    return {
      cardId,
      card,
      phase,
      hue,
      agentIds,
      isFocused,
      badgeAgent,
      titleText,
      titlePrefix,
      tags,
      issueNumber,
      prNumber,
    };
  });
}

interface DetailModalState {
  card: KanbanCard;
  phase: Phase;
  title: string;
  tags: string;
  returnFocus: HTMLElement | null;
}

interface KanbanCardButtonProps {
  resolved: ResolvedCard;
  onOpen: (resolved: ResolvedCard, returnFocus: HTMLElement) => void;
}

function KanbanCardButton({
  resolved,
  onOpen,
}: KanbanCardButtonProps): React.ReactElement {
  const {
    cardId,
    phase,
    hue,
    agentIds,
    isFocused,
    badgeAgent,
    titleText,
    titlePrefix,
    tags,
    issueNumber,
    prNumber,
  } = resolved;

  // Distinguish drag from click using pointerdown/click positions.
  const pointerStartRef = React.useRef<{ x: number; y: number } | null>(null);

  const handlePointerDown = (event: React.PointerEvent<HTMLButtonElement>) => {
    pointerStartRef.current = { x: event.clientX, y: event.clientY };
  };

  const handleClick = (event: React.MouseEvent<HTMLButtonElement>) => {
    const start = pointerStartRef.current;
    if (start) {
      const dx = Math.abs(event.clientX - start.x);
      const dy = Math.abs(event.clientY - start.y);
      if (dx > 4 || dy > 4) return;
    }
    onOpen(resolved, event.currentTarget);
  };

  let badge: React.ReactNode = null;
  if (badgeAgent) {
    const ini = agentInitials(badgeAgent.agent_id);
    let badgeStyle: React.CSSProperties = {};
    if (phase === "in_progress") {
      badgeStyle = {
        background: "var(--color-fm-busy)",
        color: "rgba(0,0,0,0.82)",
        borderColor: "var(--color-fm-busy)",
      };
    } else if (phase === "reviewing") {
      badgeStyle = {
        background: `oklch(var(--c-mark-l) var(--c-mark-c) ${hue}/0.18)`,
        borderColor: `oklch(var(--c-mark-l) var(--c-mark-c) ${hue}/0.45)`,
      };
    }
    badge = (
      <span className="km-badge" style={badgeStyle}>
        {ini}
      </span>
    );
  }

  const cardStyle: React.CSSProperties = {
    ["--c-hue" as string]: String(hue),
  };

  return (
    <button
      type="button"
      className={`km-card${isFocused ? " km-focused" : ""}`}
      data-card-id={cardId}
      data-issue={issueNumber === "" ? "" : String(issueNumber)}
      data-pr={prNumber === "" ? "" : String(prNumber)}
      data-agents={agentIds.join(" ")}
      aria-label={`Open details for ${titlePrefix} ${titleText}`}
      style={cardStyle}
      onPointerDown={handlePointerDown}
      onClick={handleClick}
    >
      <span className="km-card-main">
        <span className="km-card-title">{titleText}</span>
        {badge}
      </span>
      <span className="km-card-tags" title={tags}>
        {tags || "—"}
      </span>
      {phase === "in_progress" ? (
        <span className="km-progress-line"></span>
      ) : null}
    </button>
  );
}

interface IssueDetailModalProps {
  detail: DetailModalState | null;
  onClose: () => void;
}

interface DetailField {
  label: string;
  value: React.ReactNode;
}

interface Signal {
  title: string;
  meta: string;
  tone: "good" | "info" | "warn" | "danger" | "neutral";
}

function DetailChip({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: string;
}): React.ReactElement {
  return <span className={`issue-detail-chip ${tone}`}>{children}</span>;
}

function DetailStat({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="issue-detail-stat">
      <div className="issue-detail-label">{label}</div>
      <div className="issue-detail-stat-value">{value}</div>
    </div>
  );
}

function DetailFields({
  title,
  fields,
}: {
  title: string;
  fields: DetailField[];
}): React.ReactElement {
  return (
    <section className="issue-detail-section">
      <h3>{title}</h3>
      <dl className="issue-detail-grid">
        {fields.map((field) => (
          <React.Fragment key={field.label}>
            <dt>{field.label}</dt>
            <dd>{field.value}</dd>
          </React.Fragment>
        ))}
      </dl>
    </section>
  );
}

function DetailSignals({ signals }: { signals: Signal[] }): React.ReactElement {
  return (
    <section className="issue-detail-section">
      <h3>Current Signals</h3>
      <div className="issue-detail-signals">
        {signals.map((signal) => (
          <div className="issue-detail-signal" key={signal.title}>
            <span className={`issue-detail-signal-dot ${signal.tone}`} />
            <div>
              <div className="issue-detail-signal-title">{signal.title}</div>
              <div className="issue-detail-signal-meta">{signal.meta}</div>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function IssueDetailModal({
  detail,
  onClose,
}: IssueDetailModalProps): React.ReactElement {
  const closeBtnRef = React.useRef<HTMLButtonElement | null>(null);
  const visible = detail !== null;

  // Focus close button on open; wire ESC-to-close.
  React.useEffect(() => {
    if (!visible) return;
    closeBtnRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [visible, onClose]);

  const handleBackdropClick = (
    event: React.MouseEvent<HTMLDivElement>,
  ): void => {
    if (event.target === event.currentTarget) onClose();
  };

  const subtitleText = detail
    ? subtitleFor(detail.card, detail.phase)
    : "--";
  const titleText = detail ? detail.title : "Issue details";
  const githubUrl = detail
    ? (detail.card.issue?.url ?? detail.card.pr?.url ?? null)
    : null;

  const card = detail?.card ?? null;
  const issue = card?.issue ?? null;
  const pr = card?.pr ?? null;
  const epicTitle = card ? epicTitleFor(card) : "Unlinked";
  const beadId = card ? beadIdFor(card) : "None";
  const beadStatus = card ? beadStatusFor(card) : "Unknown";
  const mirrorStatus = card ? mirrorStatusFor(card) : "missing";
  const ready = card ? isReady(card) : false;
  const epicProgress = percentText(card?.epic?.closure_ratio);
  const tags = detail ? splitTags(detail.tags) : [];

  const issueFields: DetailField[] = [];
  const signals: Signal[] = [];
  const sidebarSections: React.ReactNode[] = [];

  if (detail) {
    if (issue) {
      issueFields.push(
        { label: "Number", value: `#${issue.issue_number}` },
        { label: "State", value: titleCase(issue.state) },
        {
          label: "Priority",
          value: issue.priority === null ? "None" : String(issue.priority),
        },
        { label: "Source", value: issue.source ?? "Unknown" },
        { label: "Created", value: formatDate(issue.created_at) },
        { label: "Closed", value: formatDate(issue.closed_at) },
      );
      signals.push({
        title: `Issue #${issue.issue_number} is ${titleCase(issue.state)}`,
        meta: `Created ${formatDate(issue.created_at)} - source ${
          issue.source ?? "Unknown"
        }`,
        tone: issue.state === "closed" ? "good" : "info",
      });
    } else {
      issueFields.push({ label: "Issue", value: "No linked issue" });
    }

    issueFields.push({
      label: "Labels",
      value:
        tags.length > 0 ? (
          <div className="issue-detail-tag-row">
            {tags.map((tag) => (
              <span className="issue-detail-tag" key={tag}>
                {tag}
              </span>
            ))}
          </div>
        ) : (
          "None"
        ),
    });

    if (card) {
      signals.push({
        title:
          beadId === "None"
            ? "No linked bead"
            : `Bead ${beadId} is ${titleCase(beadStatus)}`,
        meta: `${epicTitle} - ${ready ? "Ready" : "Not ready"} - mirror ${titleCase(
          mirrorStatus,
        ).toLowerCase()}`,
        tone: ready ? "good" : "warn",
      });
    }

    if (pr) {
      signals.push({
        title: `PR #${pr.pr_number} is ${titleCase(pr.state)}`,
        meta: `Review ${reviewText(pr).toLowerCase()} - checks ${checksText(
          pr,
        ).toLowerCase()} - ${pr.is_draft ? "draft" : "not draft"}`,
        tone: pr.blocked ? "danger" : reviewTone(pr) === "good" ? "good" : "info",
      });
    }

    if (card?.authorAgent || card?.reviewerAgent) {
      const agentMeta = [
        card.authorAgent ? `author ${agentSummary(card.authorAgent)}` : null,
        card.reviewerAgent ? `reviewer ${agentSummary(card.reviewerAgent)}` : null,
      ]
        .filter(Boolean)
        .join(" - ");
      signals.push({
        title: "Agent context attached",
        meta: agentMeta,
        tone: "neutral",
      });
    }

    sidebarSections.push(
      <DetailFields
        key="beads"
        title="Beads"
        fields={[
          { label: "Epic", value: epicTitle },
          { label: "Bead", value: beadId },
          { label: "Status", value: titleCase(beadStatus) },
          { label: "Ready", value: ready ? "Yes" : "No" },
          { label: "Mirror", value: titleCase(mirrorStatus) },
        ]}
      />,
    );

    if (pr) {
      sidebarSections.push(
        <DetailFields
          key="pr"
          title="Pull Request"
          fields={[
            { label: "Number", value: `#${pr.pr_number}` },
            { label: "Title", value: pr.title },
            { label: "State", value: titleCase(pr.state) },
            {
              label: "Branch",
              value: (
                <span className="issue-detail-branch">
                  {pr.branch ?? "Unknown"}
                </span>
              ),
            },
            { label: "Review", value: reviewText(pr) },
            { label: "Checks", value: checksText(pr) },
            { label: "Draft", value: pr.is_draft ? "Yes" : "No" },
            {
              label: "Blocked",
              value: pr.blocked
                ? pr.blocked_reasons.join(", ") || "Yes"
                : "No",
            },
            { label: "Author", value: pr.github_author ?? "Unknown" },
          ]}
        />,
      );
    }

    if (card?.authorAgent || card?.reviewerAgent) {
      const fields: DetailField[] = [];
      if (card.authorAgent) {
        fields.push({ label: "Author Agent", value: agentSummary(card.authorAgent) });
      }
      if (card.reviewerAgent) {
        fields.push({
          label: "Reviewer Agent",
          value: agentSummary(card.reviewerAgent),
        });
      }
      sidebarSections.push(
        <DetailFields key="Agents" title="Agents" fields={fields} />,
      );
    }
  }

  return (
    <div
      id="issue-detail-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="issue-detail-title"
      aria-hidden={visible ? "false" : "true"}
      className={visible ? "visible" : undefined}
      onClick={handleBackdropClick}
    >
      <div className="issue-detail-box">
        <div className="issue-detail-header">
          <div>
            <div className="issue-detail-eyebrow">
              {issue ? (
                <DetailChip tone="good">
                  {titleCase(issue.state)}
                </DetailChip>
              ) : null}
              <DetailChip tone={detail ? phaseTone(detail.phase) : "neutral"}>
                {detail ? phaseLabel(detail.phase) : "Details"}
              </DetailChip>
              {pr ? (
                <>
                  <DetailChip tone={reviewTone(pr)}>{reviewText(pr)}</DetailChip>
                  <DetailChip tone={checksTone(pr)}>{checksText(pr)}</DetailChip>
                </>
              ) : null}
              <span>{subtitleText}</span>
            </div>
            <div className="issue-detail-title" id="issue-detail-title">
              {titleText}
            </div>
          </div>
          <button
            ref={closeBtnRef}
            type="button"
            className="issue-detail-close"
            id="issue-detail-close"
            aria-label="Close issue details"
            onClick={onClose}
          >
            X
          </button>
        </div>

        <div className="issue-detail-status-strip">
          <DetailStat
            label="Priority"
            value={issue?.priority === null ? "None" : (issue?.priority ?? "None")}
          />
          <DetailStat label="Epic" value={epicTitle} />
          <DetailStat
            label="Pull Request"
            value={pr ? `#${pr.pr_number} ${reviewText(pr)}` : "No linked PR"}
          />
          <DetailStat
            label="Mirror"
            value={`${titleCase(mirrorStatus)}${ready ? " and ready" : ""}`}
          />
        </div>

        <div className="issue-detail-content" id="issue-detail-body">
          <div className="issue-detail-primary">
            <section className="issue-detail-section">
              <div className="issue-detail-summary-band">
                <div>
                  <div className="issue-detail-summary-title">
                    {card ? summaryHeadline(card) : "Issue details"}
                  </div>
                  <div className="issue-detail-summary-subtitle">
                    {tags.length > 0 ? tags.slice(0, 4).join(" - ") : subtitleText}
                  </div>
                </div>
                {epicProgress ? (
                  <div
                    className="issue-detail-score"
                    aria-label={`Epic ${epicProgress} complete`}
                  >
                    <span>{epicProgress}</span>
                    <small>epic</small>
                  </div>
                ) : null}
              </div>
            </section>

            <DetailFields title="Issue" fields={issueFields} />
            <DetailSignals signals={signals} />
          </div>

          <aside className="issue-detail-secondary">{sidebarSections}</aside>
        </div>

        <div className="issue-detail-actions">
          <div className="issue-detail-footer-meta">
            {card ? footerSummary(card) : "No active detail"}
          </div>
          <div className="issue-detail-action-buttons">
            <button
              type="button"
              className="issue-detail-action-button"
              disabled={!pr?.branch}
              onClick={() => copyText(pr?.branch)}
            >
              Copy branch
            </button>
            <button
              type="button"
              className="issue-detail-action-button"
              disabled={!issue?.issue_number}
              onClick={() => copyText(issue?.issue_number)}
            >
              Copy issue #
            </button>
            {githubUrl ? (
              <a
                className="issue-detail-link"
                id="issue-detail-open-github"
                href={githubUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open in GitHub
              </a>
            ) : (
              <a
                className="issue-detail-link"
                id="issue-detail-open-github"
                aria-disabled="true"
                hidden
              >
                Open in GitHub
              </a>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function IssueDetailModalPortal({
  detail,
  onClose,
}: IssueDetailModalProps): React.ReactElement | null {
  if (typeof document === "undefined" || !document.body) return null;
  return createPortal(
    <IssueDetailModal detail={detail} onClose={onClose} />,
    document.body,
  );
}

function buildColumns(
  state: StateUpdate | null,
): { columns: KanbanColumns; graph: ProjectGraph | null } {
  if (!state) {
    return {
      columns: { todo: [], in_progress: [], reviewing: [], done: [] },
      graph: null,
    };
  }
  const columns = deriveColumns(
    state.open_issues,
    state.agents,
    state.pull_requests,
    state.graph,
  );
  return { columns, graph: state.graph ?? null };
}

export default function KanbanStage(): React.ReactElement | null {
  const internal = store.use();
  const [detail, setDetail] = useState<DetailModalState | null>(null);

  const { columns, graph } = buildColumns(internal.state);
  const epicIds = (graph?.epics ?? []).map((epic) => epic.bead_id);

  // Open PRs hidden by the target-branch filter (base != target_branch),
  // surfaced as a header badge so they're accounted for without cluttering the board.
  const hiddenPrCount =
    internal.state?.work_availability?.pull_requests_hidden_count ?? 0;

  // Build a unique epic set in insertion order across all phases for the legend.
  const seenEpics = new Map<string, { title: string; hue: number }>();
  for (const phase of PHASES) {
    for (const card of columns[phase]) {
      if (card.epic && !seenEpics.has(card.epic.bead_id)) {
        seenEpics.set(card.epic.bead_id, {
          title: card.epic.title,
          hue: hueForEpic(card.epic.bead_id, epicIds),
        });
      }
    }
  }

  const handleOpen = (
    resolved: ResolvedCard,
    returnFocus: HTMLElement,
  ): void => {
    setDetail({
      card: resolved.card,
      phase: resolved.phase,
      title: resolved.titleText,
      tags: resolved.tags,
      returnFocus,
    });
  };

  const handleClose = (): void => {
    setDetail((current) => {
      current?.returnFocus?.focus();
      return null;
    });
  };

  const boardStyle: React.CSSProperties = {};
  if (internal.insets) {
    boardStyle.top = `${internal.insets.top}px`;
    boardStyle.left = `${internal.insets.left}px`;
    boardStyle.right = `${internal.insets.right}px`;
    boardStyle.bottom = `${internal.insets.bottom}px`;
  }

  // Reproduce the imperative module's outer "stage" wrapper (div + `hidden`)
  // so the same visibility CSS rules apply.
  return (
    <>
      <div hidden={!internal.visible}>
        <div
          className="km-board"
          style={boardStyle}
          data-focused-agent={internal.focusedAgent ?? undefined}
        >
          <div className="km-hdr">
            <span className="km-hdr-label">Issues</span>
            {hiddenPrCount > 0 && (
              <span
                className="km-hdr-hidden"
                title={`${hiddenPrCount} open PR${
                  hiddenPrCount === 1 ? "" : "s"
                } hidden — base branch differs from the session target branch`}
              >
                ({hiddenPrCount} hidden)
              </span>
            )}
            <div className="km-legend">
              {Array.from(seenEpics.entries()).map(([id, { title, hue }]) => (
                <span key={id} className="km-legend-item">
                  <span
                    className="km-legend-swatch"
                    style={{
                      background: `oklch(var(--c-mark-l) var(--c-mark-c) ${hue})`,
                    }}
                  />
                  <span>{title}</span>
                </span>
              ))}
            </div>
          </div>
          <div className="km-cols">
            {PHASES.map((phase) => {
              const cards = columns[phase];
              const countStr =
                cards.length < 10 ? `0${cards.length}` : String(cards.length);
              const resolved = resolveCardsForPhase(
                cards,
                phase,
                epicIds,
                internal.focusedAgent,
              );
              return (
                <div key={phase} className="km-col" data-phase={phase}>
                  <div className="km-col-hdr">
                    <span
                      className="km-bullet"
                      style={{ color: PHASE_BULLET_COLOR[phase] }}
                    >
                      ●
                    </span>
                    <span className="km-col-name">{PHASE_LABEL[phase]}</span>
                    <span className="km-col-count">{countStr}</span>
                  </div>
                  <div className="km-col-body">
                    {resolved.length === 0 ? (
                      <div className="km-empty">—</div>
                    ) : (
                      resolved.map((r) => (
                        <KanbanCardButton
                          key={r.cardId}
                          resolved={r}
                          onOpen={handleOpen}
                        />
                      ))
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
      <IssueDetailModalPortal detail={detail} onClose={handleClose} />
    </>
  );
}

export type { KanbanInternalState };
