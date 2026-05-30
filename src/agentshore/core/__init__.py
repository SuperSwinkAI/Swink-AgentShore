"""Core orchestrator package.

External callers import ``Orchestrator``, the dispatch/state dataclasses,
and the private helpers/phases that tests patch directly. This module
preserves every name the legacy ``agentshore.core`` single-module layout
exported so that ``patch("agentshore.core.X", ...)`` keeps working.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------
# Stdlib re-exports that tests reach for via ``patch("agentshore.core.asyncio.sleep", ...)``.
# Importing them here gives the patched-attribute path a real module to walk.
import asyncio  # noqa: F401  (re-export for test patching)

# Imports referenced from the legacy module path (``patch("agentshore.core.X")``)
from agentshore.agents.manager import AgentManager  # noqa: F401
from agentshore.core.base import _OrchestratorBase  # noqa: F401
from agentshore.core.context import _DispatchContext, _StateData
from agentshore.core.helpers import (
    _bootstrap_phase_publisher,
    _build_reward_signals,
    _cluster_just_completed,
    _compute_config_hash,
    _emit_weights_dir_inventory,
    _is_loop_bucket,
    _log_task_exception,
    _logger,
    _ppo_selector_cls,
    _step,
    _str_extra,
)
from agentshore.core.mixins.drain import SHUTDOWN_GRACE_PERIOD_SECONDS
from agentshore.core.mixins.loop import (
    _IDLE_BACKOFF_SECONDS,
    _WAITING_BACKOFF_SECONDS,
    AGENT_PING_TIMEOUT_SECONDS,
    IPC_TIMEOUT_SECONDS,
    ISSUE_REFRESH_INTERVAL_SECONDS,
)
from agentshore.core.orchestrator import Orchestrator
from agentshore.core.phases import (
    GITHUB_ISSUE_FETCH_LIMIT,
    GITHUB_PR_FETCH_LIMIT,
    _author_labels_for_config,
    _clear_session_scoped_bead_progress,
    _mirror_issues_to_beads,
    _phase_cleanup_stale_weights,
    _phase_clear_beads_in_progress,
    _phase_create_session_row,
    _phase_ensure_labels,
    _phase_fetch_github,
    _phase_git_safety_sweep,
    _phase_init_datastore,
    _phase_init_executor,
    _phase_init_metrics,
    _phase_init_ppo_selector,
    _phase_init_worktree_manager,
    _phase_install_skills,
    _phase_load_learnings,
    _phase_queue_agent_instantiation,
    _phase_reset_session_scoped_tables,
    _phase_session_start_dirty_baseline,
    _phase_session_start_worktree_sweep,
    _resolve_policy_path,
    _resolve_seed_path,
)
from agentshore.data.store import DataStore  # noqa: F401
from agentshore.logging import setup_logging  # noqa: F401
from agentshore.plays.executor import PlayExecutor  # noqa: F401
from agentshore.plays.registry import build_default_registry  # noqa: F401
from agentshore.plays.resolver import ParameterResolver  # noqa: F401

__all__ = [
    "AGENT_PING_TIMEOUT_SECONDS",
    "AgentManager",
    "DataStore",
    "GITHUB_ISSUE_FETCH_LIMIT",
    "GITHUB_PR_FETCH_LIMIT",
    "IPC_TIMEOUT_SECONDS",
    "ISSUE_REFRESH_INTERVAL_SECONDS",
    "Orchestrator",
    "ParameterResolver",
    "PlayExecutor",
    "SHUTDOWN_GRACE_PERIOD_SECONDS",
    "_DispatchContext",
    "_IDLE_BACKOFF_SECONDS",
    "_OrchestratorBase",
    "_StateData",
    "_WAITING_BACKOFF_SECONDS",
    "_author_labels_for_config",
    "_bootstrap_phase_publisher",
    "_build_reward_signals",
    "_clear_session_scoped_bead_progress",
    "_cluster_just_completed",
    "_compute_config_hash",
    "_emit_weights_dir_inventory",
    "_is_loop_bucket",
    "_log_task_exception",
    "_logger",
    "_mirror_issues_to_beads",
    "_phase_clear_beads_in_progress",
    "_phase_cleanup_stale_weights",
    "_phase_create_session_row",
    "_phase_ensure_labels",
    "_phase_fetch_github",
    "_phase_git_safety_sweep",
    "_phase_init_datastore",
    "_phase_init_executor",
    "_phase_init_metrics",
    "_phase_init_ppo_selector",
    "_phase_init_worktree_manager",
    "_phase_install_skills",
    "_phase_load_learnings",
    "_phase_queue_agent_instantiation",
    "_phase_reset_session_scoped_tables",
    "_phase_session_start_dirty_baseline",
    "_phase_session_start_worktree_sweep",
    "_ppo_selector_cls",
    "_resolve_policy_path",
    "_resolve_seed_path",
    "_step",
    "_str_extra",
    "build_default_registry",
    "setup_logging",
]
