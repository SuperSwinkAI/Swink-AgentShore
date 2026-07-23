import React, { useState } from "react";
import type { AgentSnapshot, ProjectGraph, StateUpdate } from "../types";
import {
  deriveColumns,
  PHASES,
  PHASE_LABEL,
  type KanbanCard,
  type KanbanColumns,
  type Phase,
} from "../views/kanban/phase";
import { createNotifyStore } from "../notifyStore";
import {
  IssueDetailModal,
  type DetailModalState,
} from "./kanban/IssueDetailModal";

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
      <IssueDetailModal detail={detail} onClose={handleClose} />
    </>
  );
}

export type { KanbanInternalState };
