import { callJsonRpc } from "./jsonrpc";

/**
 * Live (in-session) budget RPC client. Mirrors ``projectClient.setBudget``
 * (which persists to agentshore.yaml via ``project.set_budget``) but targets
 * the running session's budget controller through ``session.set_budget`` /
 * ``session.get_budget``. The desktop "Adjust Budget…" menu (issue #43) uses
 * these to re-cap an in-flight session without restarting it.
 */

/**
 * Payload shape for ``session.set_budget``. The dollar and time soft caps are
 * independent. Matches ``budgetSelectionToConfig`` in ``setup/projectYaml.ts``.
 */
export interface BudgetRpcInput {
  enabled: boolean;
  total: number;
  time_enabled: boolean;
  time_total_minutes: number;
}

/**
 * The applied budget echoed back by ``session.set_budget`` /
 * ``session.get_budget``. Includes live consumption (spent/remaining,
 * elapsed/remaining minutes) on top of the configured caps.
 */
export interface AppliedBudget {
  enabled: boolean;
  total: number;
  spent: number;
  remaining: number;
  time_enabled: boolean;
  time_total_minutes: number;
  time_elapsed_minutes: number;
  time_remaining_minutes: number;
}

export interface BudgetRpcResult {
  budget: AppliedBudget;
}

/** Apply a new budget to the running session (``session.set_budget``). */
export async function setBudgetLive(
  budget: BudgetRpcInput,
): Promise<BudgetRpcResult> {
  return callJsonRpc<BudgetRpcResult>("session.set_budget", { budget });
}

/** Read the running session's current budget (``session.get_budget``). */
export async function getBudget(): Promise<BudgetRpcResult> {
  return callJsonRpc<BudgetRpcResult>("session.get_budget");
}
