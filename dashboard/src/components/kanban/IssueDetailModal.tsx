import React from "react";
import { createPortal } from "react-dom";
import type { AgentSnapshot, PullRequestSnapshot } from "../../types";
import type { KanbanCard, Phase } from "../../views/kanban/phase";
import { titleCase } from "../../format";

// Helpers mirror render.ts/detailModal.ts.

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

export interface DetailModalState {
  card: KanbanCard;
  phase: Phase;
  title: string;
  tags: string;
  returnFocus: HTMLElement | null;
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

function IssueDetailModalContent({
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

export function IssueDetailModal({
  detail,
  onClose,
}: IssueDetailModalProps): React.ReactElement | null {
  if (typeof document === "undefined" || !document.body) return null;
  return createPortal(
    <IssueDetailModalContent detail={detail} onClose={onClose} />,
    document.body,
  );
}
