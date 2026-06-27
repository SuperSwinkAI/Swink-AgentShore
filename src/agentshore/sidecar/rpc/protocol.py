"""JSON-RPC 2.0 wire types, error codes, factory helpers, and session state.

Everything in this module is pure-data / pure-construction — no I/O, no
asyncio, no imports from other sidecar sub-modules (except TYPE_CHECKING
guards for store/bridge types).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from agentshore.data.store import DataStore
    from agentshore.sidecar.embedded_bridge import EmbeddedBridge
    from agentshore.sidecar.session_lifecycle import OrchestratorHandle

# ---------------------------------------------------------------------------
# Error-code constants (DESIGN §5.1)
# ---------------------------------------------------------------------------

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
REQUEST_CANCELLED = -32800
ERR_SESSION_ACTIVE = -32010
ERR_NO_ACTIVE_PROJECT = -32011


# ---------------------------------------------------------------------------
# Session-state dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SessionContext:
    """Per-session handles passed by the embedded bridge to the RPC server.

    Populated when ``session.start`` succeeds so ``session.stop`` can build a
    rich ESR payload. Left ``None`` outside an active session.
    """

    session_id: str
    store: DataStore
    archive_path: str
    report_path: str
    log_path: str | None


@dataclass
class ServerState:
    """In-memory lifecycle state shared across requests on one sidecar instance."""

    active_project_path: str | None = None
    session_active: bool = False
    session_id: str | None = None
    started_at: str | None = None
    ipc_endpoint: dict[str, object] | None = None
    session_context: SessionContext | None = None
    data_store: DataStore | None = None
    # Set when the dashboard bridge boots; session.stop tears it down first.
    bridge: EmbeddedBridge | None = None
    # session.start boots a real Orchestrator (DESIGN §5.1); session.stop drains via
    # these handles. Typed as the TYPE_CHECKING-only OrchestratorHandle Protocol to
    # keep the engine import lazy (cold-start torch-free invariant).
    orchestrator: OrchestratorHandle | None = None
    orchestrator_task: asyncio.Task[None] | None = None
    # In-flight orch.stop() teardown (#283). Its DataStore.close() holds the SQLite
    # writer lock for seconds (backup snapshot + os.replace); a restart must await
    # this or the new store_init hits "database is locked".
    store_teardown_task: asyncio.Task[None] | None = None
    esr_ready_report_path: str | None = None
    esr_ready_log_path: str | None = None
    # Optional timelapse capture (desktop). run-id + shared cwd it must resolve
    # against; session.stop ends capture and attaches the MP4 to the ESR payload.
    timelapse_run_id: str | None = None
    timelapse_runs_cwd: Path | None = None


# ---------------------------------------------------------------------------
# Wire TypedDicts
# ---------------------------------------------------------------------------


class JsonRpcError(TypedDict):
    code: int
    message: str


class JsonRpcResponse(TypedDict, total=False):
    jsonrpc: str
    id: int | str | None
    result: object
    error: JsonRpcError


class JsonRpcNotification(TypedDict):
    jsonrpc: str
    method: str
    params: dict[str, object]


DispatchResult = JsonRpcResponse | Awaitable[JsonRpcResponse] | None

MethodHandler = Callable[[dict[str, object]], object | Awaitable[object]]

RouteHandler = Callable[..., DispatchResult]


# ---------------------------------------------------------------------------
# Response / notification factories
# ---------------------------------------------------------------------------


def _error(req_id: int | str | None, code: int, message: str) -> JsonRpcResponse:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _result(req_id: int | str | None, result: object) -> JsonRpcResponse:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def notification(method: str, params: dict[str, object]) -> JsonRpcNotification:
    """Build a JSON-RPC 2.0 notification (sidecar → shell).

    The single factory every notification builder routes through, so the
    ``{"jsonrpc": "2.0", "method": ..., "params": ...}`` envelope is authored
    in exactly one place (DESIGN §5.1).
    """
    return {"jsonrpc": "2.0", "method": method, "params": params}


# ---------------------------------------------------------------------------
# Param helpers
# ---------------------------------------------------------------------------


class _ParamError(Exception):
    """Raised inside dispatch when params fail shape validation."""


def _as_dict(params: object) -> dict[str, object]:
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    raise _ParamError("params must be an object")


# ---------------------------------------------------------------------------
# Project-method error-code remap table
# ---------------------------------------------------------------------------

# Remaps project_rpc.ERR_PROJECT_NOT_ACTIVE (-32004) → public ERR_NO_ACTIVE_PROJECT
# (-32011). select/deselect skip _require_active; everything else calls it.
_PROJECT_NO_ACTIVE_REMAP = frozenset(
    {
        "project.inspect",
        "project.branches",
        "project.set_target_branch",
        "project.set_seed_paths",
        "project.set_budget",
        "project.set_trusted_issue_enforcement",
        "project.set_timelapse",
        "project.install_timelapse",
    }
)


# ---------------------------------------------------------------------------
# Optional dedup helper (behavior-preserving — identical body in both callers)
# ---------------------------------------------------------------------------


def _require_str_params(raw_params: object, *fields: str) -> tuple[dict[str, object], list[str]]:
    """Validate that ``raw_params`` is a dict and extract the named string fields.

    Returns ``(params_dict, missing_fields)``.  Callers check ``missing_fields``
    and emit ``INVALID_PARAMS`` when it is non-empty.  Raises ``_ParamError``
    when ``raw_params`` is not a mapping.
    """
    if not isinstance(raw_params, dict):
        raise _ParamError("params must be an object")
    missing = [f for f in fields if not isinstance(raw_params.get(f), str)]
    return raw_params, missing
