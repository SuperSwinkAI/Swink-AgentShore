"""Beads integration — public re-exports over the beads package's submodules.

Beads (bd) is the project-graph store: epics own stories own tasks. All
calls shell out to the `bd` binary via asyncio subprocesses; there is no
wrapper class. This package is split by concern:

  lock.py         — reader/writer lock, ``bd()`` subprocess core, binary
                    resolution, agent-dispatch PATH shim
  types.py        — enums and dataclasses for the beads project graph
  parsing.py      — JSON boundary parsing (raw bd JSON -> typed dataclasses)
  graph.py        — graph loading, epic/task aggregation, snapshot cache
  session.py      — session-scoped ``in_progress`` cleanup
  dependencies.py — dependency-cycle mutation (``add_blocking_dependency``)
  memory.py       — kv-store-backed persistent memory helpers

Everything below is re-exported here so existing call sites
(``from agentshore.beads import ...``) keep working unchanged.

Three-layer architecture:
  BEADS   — project graph (this package talks to it)
  GITHUB  — human-facing issues (mirrored via external_ref = "gh-N")
  SQLITE  — session-scoped RL state (plays, experience, agents)
"""

from __future__ import annotations

from agentshore.beads.dependencies import (
    BlockingDependencyOutcome,
    _would_create_cycle,
    add_blocking_dependency,
)
from agentshore.beads.graph import (
    _BD_GRAPH_TIMEOUT_SECONDS,
    _GRAPH_CACHE_TTL_SECONDS,
    _GRAPH_READ_RETRIES,
    load_graph,
    ready_tasks,
)
from agentshore.beads.lock import (
    BD_TIMEOUT_SECONDS,
    BdError,
    BdTimeoutError,
    BeadsSchemaDriftError,
    GraphReadError,
    _ReadersWriterLock,
    bd,
    ensure_bd_on_agent_path,
    is_schema_drift_error,
    resolve_bd_binary,
)
from agentshore.beads.memory import remember
from agentshore.beads.session import clear_in_progress_beads
from agentshore.beads.types import (
    Bead,
    BeadStatus,
    BeadType,
    EpicStatus,
    GraphTask,
    ProjectGraph,
    pick_bead_for_issue,
)

__all__ = [
    "BD_TIMEOUT_SECONDS",
    "Bead",
    "BeadStatus",
    "BeadType",
    "BeadsSchemaDriftError",
    "BdError",
    "BdTimeoutError",
    "BlockingDependencyOutcome",
    "EpicStatus",
    "GraphReadError",
    "GraphTask",
    "ProjectGraph",
    "add_blocking_dependency",
    "bd",
    "clear_in_progress_beads",
    "ensure_bd_on_agent_path",
    "is_schema_drift_error",
    "load_graph",
    "pick_bead_for_issue",
    "ready_tasks",
    "remember",
    "resolve_bd_binary",
    # Leading-underscore names below are not part of the public API; they are
    # re-exported only because tests import/patch them directly at these
    # top-level paths (see beads/lock.py, beads/graph.py for the real owners).
    "_BD_GRAPH_TIMEOUT_SECONDS",
    "_GRAPH_CACHE_TTL_SECONDS",
    "_GRAPH_READ_RETRIES",
    "_ReadersWriterLock",
    "_would_create_cycle",
]
