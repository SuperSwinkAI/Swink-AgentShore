/**
 * Minimal parser for the slice of agentshore.yaml the desktop setup flow
 * needs to hydrate previous-session choices (DESIGN §10.1 re-entry policy).
 *
 * We intentionally do not depend on a full YAML library. The desktop only
 * reads a handful of top-level fields, all with stable shapes:
 *
 * - ``project.target_branch`` (string)
 * - ``agents.<runner>.enabled`` (boolean) → enabled-runner set
 * - ``identities`` (mapping; the keys are GitHub login names)
 * - ``budget.enabled`` (boolean) and ``budget.total`` (number)
 *
 * The parser tokenises lines into indent + key + value and walks the two-
 * space-indent tree agentshore.yaml uses. Anything outside the recognised
 * sections is ignored. The parser is intentionally forgiving: bad
 * structure returns an empty hydration rather than throwing, so a
 * partially-corrupt yaml never blocks the user from reaching the setup
 * rail.
 */

export interface BudgetHydration {
  /** ``true`` when ``budget.enabled: true`` parsed cleanly; ``false`` for
   *  explicit ``false`` or absence of the key. */
  enabled: boolean;
  /** Dollars from ``budget.total``; null when absent or not a finite
   *  non-negative number. */
  totalUsd: number | null;
}

export interface TimelapseHydration {
  /** ``timelapse.enabled`` — the per-project default capture toggle. */
  enabled: boolean;
  /** ``timelapse.installed`` — the CLI + deps were provisioned. */
  installed: boolean;
}

export interface ProjectYamlHydration {
  targetBranch: string | null;
  enabledAgents: string[];
  identityLogins: string[];
  budget: BudgetHydration | null;
  timelapse: TimelapseHydration | null;
}

const EMPTY: ProjectYamlHydration = {
  targetBranch: null,
  enabledAgents: [],
  identityLogins: [],
  budget: null,
  timelapse: null,
};

interface ParsedLine {
  indent: number;
  key: string;
  /** Inline scalar after ``key:``; null when the value continues on a
   *  child block. */
  value: string | null;
}

function parseLine(raw: string): ParsedLine | null {
  // Strip trailing whitespace; ignore blank lines and full-line comments.
  const noTrail = raw.replace(/\s+$/u, "");
  if (noTrail.length === 0) return null;
  const trimmedLeft = noTrail.replace(/^\s+/u, "");
  if (trimmedLeft.startsWith("#")) return null;
  const indent = noTrail.length - trimmedLeft.length;
  // Skip array items — they never appear at the depths we care about
  // for the hydrated fields and would confuse the indent walker.
  if (trimmedLeft.startsWith("-")) return null;
  const colon = trimmedLeft.indexOf(":");
  if (colon === -1) return null;
  const key = trimmedLeft.slice(0, colon).trim();
  if (key.length === 0) return null;
  // Strip inline ``# comment`` then trim.
  const after = trimmedLeft.slice(colon + 1);
  const commentIdx = after.indexOf("#");
  const rhs = (commentIdx === -1 ? after : after.slice(0, commentIdx)).trim();
  return { indent, key, value: rhs.length === 0 ? null : stripQuotes(rhs) };
}

function stripQuotes(s: string): string {
  if (s.length >= 2) {
    const first = s.charAt(0);
    const last = s.charAt(s.length - 1);
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return s.slice(1, -1);
    }
  }
  return s;
}

function asBool(value: string | null): boolean | null {
  if (value === null) return null;
  if (value === "true") return true;
  if (value === "false") return false;
  return null;
}

/**
 * Parse a raw agentshore.yaml text and extract the desktop-relevant fields.
 *
 * Returns ``EMPTY`` (all fields blank) when the input is missing or
 * unparseable so the caller can safely merge it into a setup state.
 */
export function parseProjectYaml(raw: string | null | undefined): ProjectYamlHydration {
  if (!raw || raw.trim().length === 0) return EMPTY;

  const lines = raw.split(/\r?\n/u).map(parseLine);

  const result: ProjectYamlHydration = {
    targetBranch: null,
    enabledAgents: [],
    identityLogins: [],
    budget: null,
    timelapse: null,
  };

  // Single pass; track which top-level section we are inside and, for the
  // ``agents`` block, which runner we are inside.
  type TopSection = "project" | "agents" | "identities" | "budget" | "timelapse" | null;
  let topSection: TopSection = null;
  let currentAgent: string | null = null;
  let currentAgentEnabled: boolean | null = null;
  // Budget fields are accumulated into a scratch object and only promoted
  // to ``result.budget`` if at least one recognised key parsed cleanly —
  // a budget block that only contained ``warning_threshold`` should still
  // leave hydration's budget as null so callers fall back to their own
  // defaults.
  let budgetSeen = false;
  let budgetEnabled = false;
  let budgetTotal: number | null = null;
  let timelapseSeen = false;
  let timelapseEnabled = false;
  let timelapseInstalled = false;

  const closeAgent = () => {
    if (currentAgent && currentAgentEnabled === true) {
      if (!result.enabledAgents.includes(currentAgent)) {
        result.enabledAgents.push(currentAgent);
      }
    }
    currentAgent = null;
    currentAgentEnabled = null;
  };

  for (const line of lines) {
    if (line === null) continue;

    if (line.indent === 0) {
      // Closing the previous agent (if any) before changing sections.
      closeAgent();
      if (line.key === "project") {
        topSection = "project";
      } else if (line.key === "agents") {
        topSection = "agents";
      } else if (line.key === "identities") {
        topSection = "identities";
      } else if (line.key === "budget") {
        topSection = "budget";
      } else if (line.key === "timelapse") {
        topSection = "timelapse";
      } else {
        topSection = null;
      }
      continue;
    }

    if (topSection === "timelapse" && line.indent === 2) {
      if (line.key === "enabled") {
        const v = asBool(line.value);
        if (v !== null) {
          timelapseEnabled = v;
          timelapseSeen = true;
        }
      } else if (line.key === "installed") {
        const v = asBool(line.value);
        if (v !== null) {
          timelapseInstalled = v;
          timelapseSeen = true;
        }
      }
      continue;
    }

    if (topSection === "budget" && line.indent === 2) {
      if (line.key === "enabled") {
        const v = asBool(line.value);
        if (v !== null) {
          budgetEnabled = v;
          budgetSeen = true;
        }
      } else if (line.key === "total" && line.value !== null) {
        const n = Number.parseFloat(line.value);
        if (Number.isFinite(n) && n >= 0) {
          budgetTotal = n;
          budgetSeen = true;
        }
      }
      continue;
    }

    if (topSection === "project" && line.indent === 2 && line.key === "target_branch") {
      if (line.value !== null) result.targetBranch = line.value;
      continue;
    }

    if (topSection === "agents") {
      if (line.indent === 2) {
        closeAgent();
        currentAgent = line.key;
        currentAgentEnabled = null;
      } else if (line.indent === 4 && line.key === "enabled" && currentAgent !== null) {
        const v = asBool(line.value);
        if (v !== null) currentAgentEnabled = v;
      }
      continue;
    }

    if (topSection === "identities" && line.indent === 2) {
      if (!result.identityLogins.includes(line.key)) {
        result.identityLogins.push(line.key);
      }
      continue;
    }
  }

  // Flush the trailing agent if the file ended mid-agents block.
  closeAgent();

  if (budgetSeen) {
    result.budget = {
      enabled: budgetEnabled,
      totalUsd: budgetTotal,
    };
  }

  if (timelapseSeen) {
    result.timelapse = {
      enabled: timelapseEnabled,
      installed: timelapseInstalled,
    };
  }

  return result;
}

/**
 * Re-shape a ``BudgetHydration`` into the SetupState ``budget`` shape the
 * desktop carries on the rail. Useful when overlaying parsed yaml on top
 * of localStorage state. Returns ``null`` when the input has nothing
 * actionable (the caller should keep its current value).
 */
export function budgetHydrationToSelection(
  hydration: BudgetHydration | null,
): { mode: "capped" | "unlimited"; total: number } | null {
  if (hydration === null) return null;
  if (hydration.enabled) {
    // total <= 0 is invalid for an enabled budget (the backend enforces
    // MIN_ENABLED_BUDGET_USD=20). Surface enabled but let the screen
    // clamp the value into the slider's range.
    return { mode: "capped", total: hydration.totalUsd ?? 0 };
  }
  return { mode: "unlimited", total: 0 };
}

/**
 * Serialize a SetupState budget into the ``BudgetConfig`` shape that
 * agentshore.yaml stores (matches ``src/agentshore/config/models.py:92``).
 * ``Capped`` mode flips ``enabled: true`` and writes the dollar amount;
 * ``Unlimited`` clears the cap by writing ``enabled: false, total: 0.0``,
 * which is also the dataclass default.
 */
export function budgetSelectionToConfig(selection: {
  mode: "capped" | "unlimited";
  total: number;
}): { enabled: boolean; total: number } {
  if (selection.mode === "capped") {
    return { enabled: true, total: selection.total };
  }
  return { enabled: false, total: 0.0 };
}
