"""Real ``session.start`` preparation sequence.

DESIGN §10.2 specifies six canonical bringup phases that the desktop
Screen 8 checklist mirrors:

1. ``config_merge`` — load ``agentshore.yaml`` for the active project.
2. ``install_skills`` — ensure project-level skill templates are current.
3. ``init_beads`` — ensure the beads graph is initialised.
4. ``bind_ipc`` — reserve a TCP loopback endpoint for the orchestrator.
5. ``start_bridge`` — boot the WebSocket dashboard bridge.
6. ``first_snapshot`` — wait until state can flow to the dashboard.

Each phase emits a ``running`` ``$/progress`` notification, runs its work,
then emits an ``ok`` notification on success or a ``failed`` notification
on error. The first failing phase short-circuits the runner: subsequent
phases are skipped and ``SessionStartError`` propagates so the dispatcher
can translate it into a JSON-RPC error response.

The active-project requirement is the binding constraint: phases run real
work only when ``ServerState.active_project_path`` is set. With no active
project the runner falls back to the legacy stub behaviour (emit all
phases as ok) so older tests and pre-projectselect call sites keep working
during the transition to project-scoped sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from agentshore.session_path import (
    IpcEndpoint,
    find_free_tcp_port,
    session_dir,
    write_pid,
    write_session_info,
)
from agentshore.sidecar.embedded_bridge import EmbeddedBridge

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentshore.config import RuntimeConfig
    from agentshore.sidecar.server import JsonRpcNotification, ServerState
    from agentshore.state import AgentType

_logger = structlog.get_logger(__name__)

# Default time the drain path waits for in-flight plays to complete before
# falling back to a hard cancel during session.stop. Set as a constant so
# tests can override via the kwarg; not yet promoted to ``agentshore.yaml``.
DEFAULT_DRAIN_TIMEOUT_SECONDS: float = 300.0

# Cap on how long ``first_snapshot`` waits for the orchestrator to publish
# its first state. ``Orchestrator.publish_initial_state`` runs in the boot
# task and completes immediately for empty projects; longer waits indicate
# a stuck bootstrap, so we surface as a phase failure rather than block
# session.start indefinitely.
DEFAULT_FIRST_SNAPSHOT_TIMEOUT_SECONDS: float = 30.0


# Canonical step IDs must stay in lockstep with
# ``desktop/src/startupSteps.ts:STARTUP_STEP_IDS``.
STEP_CONFIG_MERGE = "config_merge"
STEP_INSTALL_SKILLS = "install_skills"
STEP_INIT_BEADS = "init_beads"
STEP_BIND_IPC = "bind_ipc"
STEP_START_BRIDGE = "start_bridge"
STEP_FIRST_SNAPSHOT = "first_snapshot"

SESSION_START_STEP_IDS: tuple[str, ...] = (
    STEP_CONFIG_MERGE,
    STEP_INSTALL_SKILLS,
    STEP_INIT_BEADS,
    STEP_BIND_IPC,
    STEP_START_BRIDGE,
    STEP_FIRST_SNAPSHOT,
)


# Mapping of step id → user-facing label. The ``running`` notification adds
# an ellipsis suffix; the ``ok`` notification reuses the label verbatim.
_STEP_LABELS: dict[str, str] = {
    STEP_CONFIG_MERGE: "Config merged",
    STEP_INSTALL_SKILLS: "Skills installed",
    STEP_INIT_BEADS: "Beads ready",
    STEP_BIND_IPC: "IPC endpoint bound",
    STEP_START_BRIDGE: "Dashboard bridge starting",
    STEP_FIRST_SNAPSHOT: "First state snapshot",
}


@dataclass
class SessionStartOutcome:
    """Result of a successful ``session.start`` preparation."""

    session_id: str
    started_at: str
    ipc_endpoint: dict[str, object]


class SessionStartError(Exception):
    """Raised when a preparation phase fails.

    Carries the failing step id and a JSON-RPC error code the dispatcher
    can translate. ``step`` is one of the canonical step ids; ``code`` is
    the numeric JSON-RPC error code (e.g. ``-32602`` for INVALID_PARAMS,
    ``-32011`` for ERR_NO_ACTIVE_PROJECT).
    """

    def __init__(self, step: str, code: int, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.code = code


def _progress(
    token: object,
    step: str,
    status: str,
    *,
    error: str | None = None,
) -> JsonRpcNotification:
    """Build a ``$/progress`` notification for a phase outcome.

    ``status`` is one of ``"running"`` / ``"ok"`` / ``"failed"``. The
    ``percent`` field stays consistent with the start-of-phase ``0`` and
    end-of-phase ``100`` pattern so the desktop step checklist can derive
    the transition without parsing the status text.
    """
    label = _STEP_LABELS.get(step, step)
    if status == "running":
        message = f"{label}…"
        percent = 0
    elif status == "failed":
        message = error or f"{label} failed"
        percent = 100
    else:
        message = label
        percent = 100
    params: dict[str, object] = {
        "token": token,
        "step": step,
        "percent": percent,
        "message": message,
    }
    if status == "failed":
        params["error"] = error or message
    return {
        "jsonrpc": "2.0",
        "method": "$/progress",
        "params": params,
    }


def _emit(
    notify: Callable[[JsonRpcNotification], None] | None,
    token: object | None,
    step: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    if notify is None or token is None:
        return
    notify(_progress(token, step, status, error=error))


def _check_config_merge(project_path: Path) -> RuntimeConfig:
    """Load and return the merged AgentShore config for ``project_path``.

    Replaces the previous existence-only check: ``load_config`` parses the
    YAML, validates types, and surfaces any malformed-config error early
    so the dispatcher returns a structured failure rather than the
    orchestrator crashing on bootstrap.
    """
    config_path = project_path / "agentshore.yaml"
    if not config_path.exists():
        msg = f"agentshore.yaml not found at {config_path}"
        raise SessionStartError(STEP_CONFIG_MERGE, -32602, msg)
    # Lazy import: keeps the cold-start torch-free invariant intact —
    # agentshore.config is light, but importing here mirrors the orchestrator
    # boot pattern below.
    from agentshore.config import load_config
    from agentshore.errors import ConfigError

    try:
        return load_config(config_path)
    except ConfigError as exc:
        raise SessionStartError(STEP_CONFIG_MERGE, -32602, str(exc)) from exc


def _run_install_skills(project_path: Path) -> None:
    """Install bundled AgentShore skill templates into ``.agents/skills/``.

    Idempotent — ``install_skills`` only overwrites stale templates whose
    ``agentshore_version`` frontmatter is older than the bundled version.
    Failures are reclassified as a phase failure so the dispatcher can
    return a structured error.
    """
    from agentshore.skills import install_skills

    try:
        install_skills(project_path)
    except (OSError, ValueError) as exc:
        msg = f"failed to install skill templates: {exc}"
        raise SessionStartError(STEP_INSTALL_SKILLS, -32603, msg) from exc


def _enabled_agent_types_from_config(cfg: RuntimeConfig | None) -> set[AgentType]:
    """Extract enabled agent types from a loaded config, defaulting to Claude Code."""
    from agentshore.config.models import AgentConfig
    from agentshore.state import AgentType

    if cfg is None:
        return {AgentType.CLAUDE_CODE}
    valid_values = {at.value for at in AgentType}
    try:
        enabled = {
            AgentType(key)
            for key, agent_cfg in cfg.agents.items()
            if isinstance(agent_cfg, AgentConfig) and agent_cfg.enabled and key in valid_values
        }
        return enabled
    except (ValueError, AttributeError):
        return {AgentType.CLAUDE_CODE}


async def _run_init_beads(project_path: Path, cfg: RuntimeConfig | None = None) -> None:
    """Initialise the beads project graph and install git hooks.

    Mirrors the CLI's ``run_beads_init`` sequence:
      1. ``bd_init_project`` — run ``bd init`` when ``.beads/`` is absent.
      2. ``bd_setup_for_agent_types`` — run ``bd hooks install`` (best-effort).

    Step 1 failure is fatal (blocks session.start).  Step 2 failure is
    logged but not propagated — hooks can be installed later.
    """
    from agentshore.beads.setup import bd_init_project, bd_setup_for_agent_types

    beads_dir = project_path / ".beads"
    try:
        await bd_init_project(project_path)
    except Exception as exc:
        msg = f".beads/ directory not found at {beads_dir} and automatic init failed: {exc}"
        raise SessionStartError(STEP_INIT_BEADS, -32602, msg) from exc

    enabled_types = _enabled_agent_types_from_config(cfg)
    try:
        await bd_setup_for_agent_types(project_path, enabled_types)
    except Exception as exc:
        _logger.warning("bd_hooks_install_skipped", error=str(exc))


def _allocate_ipc_endpoint() -> dict[str, object]:
    """Pick a free loopback TCP port for the orchestrator IPC channel."""
    port = find_free_tcp_port("127.0.0.1")
    endpoint = IpcEndpoint.tcp("127.0.0.1", port)
    return endpoint.to_json()


async def run_session_start(
    state: ServerState,
    *,
    progress_token: object | None = None,
    notify: Callable[[JsonRpcNotification], None] | None = None,
    start_bridge: bool = True,
    start_orchestrator: bool = False,
    first_snapshot_timeout_seconds: float = DEFAULT_FIRST_SNAPSHOT_TIMEOUT_SECONDS,
    seed_path: str | None = None,
) -> SessionStartOutcome:
    """Execute the six-phase ``session.start`` bringup (async).

    Real preparation work runs only when ``state.active_project_path`` is
    set. Without an active project the runner emits each phase as ok and
    returns a stub outcome so the legacy "no project selected" code path
    keeps working. With an active project, each phase runs real
    validation / allocation; the first failure short-circuits with a
    ``SessionStartError``.

    ``start_bridge``: when True (default), the ``start_bridge`` phase
    constructs and starts an :class:`EmbeddedBridge` task in the current
    event loop and writes the handle to ``state.bridge`` so
    ``session.stop`` can tear it down. Tests that don't want to bind a
    real WebSocket port pass ``start_bridge=False``.

    ``start_orchestrator``: when True (and a project_path is set), the
    ``first_snapshot`` phase boots a real :class:`agentshore.core.Orchestrator`
    inside the current event loop, wires its file-backed state provider
    to the session_dir the bridge is tailing, and supervises
    ``run_until_idle`` as an asyncio task. ``session.stop`` then drives
    drain/hard shutdown through ``state.orchestrator``. Defaults to
    False so existing bridge-only tests don't pay the PPO bootstrap
    cost; the sidecar's stdio dispatcher passes True (DESIGN §5.1).

    The caller is responsible for translating ``SessionStartError`` into
    a JSON-RPC error response and persisting the returned ``session_id``
    and ``ipc_endpoint`` onto ``state``.
    """
    project_path: Path | None = (
        Path(state.active_project_path) if state.active_project_path else None
    )
    session_id = state.session_id or str(uuid.uuid4())
    state.session_id = session_id

    # Phase 1: config_merge
    _emit(notify, progress_token, STEP_CONFIG_MERGE, "running")
    cfg: RuntimeConfig | None = None
    if project_path is not None:
        try:
            cfg = _check_config_merge(project_path)
        except SessionStartError as exc:
            _emit(notify, progress_token, exc.step, "failed", error=str(exc))
            raise
    _emit(notify, progress_token, STEP_CONFIG_MERGE, "ok")

    # Phase 2: install_skills — copy bundled skill templates into
    # ``.agents/skills/``. Idempotent: only stale templates are
    # overwritten. Skipped when there's no active project so the
    # legacy stub-mode call path stays a no-op.
    _emit(notify, progress_token, STEP_INSTALL_SKILLS, "running")
    if project_path is not None:
        try:
            _run_install_skills(project_path)
        except SessionStartError as exc:
            _emit(notify, progress_token, exc.step, "failed", error=str(exc))
            raise
    _emit(notify, progress_token, STEP_INSTALL_SKILLS, "ok")

    # Phase 3: init_beads
    _emit(notify, progress_token, STEP_INIT_BEADS, "running")
    if project_path is not None:
        try:
            await _run_init_beads(project_path, cfg=cfg)
        except SessionStartError as exc:
            _emit(notify, progress_token, exc.step, "failed", error=str(exc))
            raise
    _emit(notify, progress_token, STEP_INIT_BEADS, "ok")

    # Phase 4: bind_ipc — always allocates a fresh endpoint, even without
    # an active project.
    _emit(notify, progress_token, STEP_BIND_IPC, "running")
    if state.ipc_endpoint is None:
        state.ipc_endpoint = _allocate_ipc_endpoint()
    _emit(notify, progress_token, STEP_BIND_IPC, "ok")

    # Phase 5: start_bridge — boot the EmbeddedBridge as a supervised
    # asyncio task. The bridge serves empty state until the orchestrator
    # publishes to the IPC endpoint allocated above. Failure here (e.g.
    # port collision, missing static bundle) surfaces as a structured
    # ``failed`` $/progress event followed by a SessionStartError.
    _emit(notify, progress_token, STEP_START_BRIDGE, "running")
    if start_bridge and project_path is not None and state.bridge is None:
        try:
            bridge = _make_bridge(project_path, state.ipc_endpoint)
            await bridge.start()
        except Exception as exc:
            msg = f"failed to start dashboard bridge: {exc}"
            _emit(notify, progress_token, STEP_START_BRIDGE, "failed", error=msg)
            raise SessionStartError(STEP_START_BRIDGE, -32603, msg) from exc
        state.bridge = bridge
    _emit(notify, progress_token, STEP_START_BRIDGE, "ok")

    # Phase 6: first_snapshot — when the orchestrator is enabled, this
    # phase boots it and waits for the first state publish to reach the
    # bridge's state_dir. When disabled, the bridge-ready signal from
    # phase 5 is already sufficient evidence that the dashboard can
    # subscribe; we emit ``ok`` immediately.
    _emit(notify, progress_token, STEP_FIRST_SNAPSHOT, "running")
    if start_orchestrator and project_path is not None and cfg is not None:
        try:
            await _start_orchestrator(
                state=state,
                project_path=project_path,
                cfg=cfg,
                session_id=session_id,
                notify=notify,
                first_snapshot_timeout_seconds=first_snapshot_timeout_seconds,
                seed_path=seed_path,
            )
        except SessionStartError as exc:
            _emit(notify, progress_token, exc.step, "failed", error=str(exc))
            raise
        except Exception as exc:
            msg = f"failed to start orchestrator: {exc}"
            _emit(notify, progress_token, STEP_FIRST_SNAPSHOT, "failed", error=msg)
            raise SessionStartError(STEP_FIRST_SNAPSHOT, -32603, msg) from exc
    _emit(notify, progress_token, STEP_FIRST_SNAPSHOT, "ok")

    started_at = state.started_at or datetime.now(UTC).isoformat()
    state.started_at = started_at

    # Make the desktop-mode session discoverable by the CLI's
    # `agentshore stop` (desktop-r3o6). CLI sessions write
    # ~/.config/swink/agentshore/sessions/<hash>/agentshore.pid + info.json with the IPC
    # endpoint; the desktop sidecar took a separate path through
    # session.start RPC and skipped this registration entirely, so
    # `agentshore stop /path/to/project` reported "no running session".
    # Mirror the same writes here when an active project is set so
    # both stop paths converge.
    if project_path is not None:
        ipc_for_info: IpcEndpoint | None = None
        if isinstance(state.ipc_endpoint, dict):
            kind = state.ipc_endpoint.get("kind")
            host = state.ipc_endpoint.get("host")
            port = state.ipc_endpoint.get("port")
            if kind == "tcp" and isinstance(host, str) and isinstance(port, int):
                ipc_for_info = IpcEndpoint.tcp(host, port)
        with contextlib.suppress(OSError):
            write_pid(project_path)
            write_session_info(
                project_path,
                ipc_endpoint=ipc_for_info,
                extra={"mode": "desktop", "session_id": session_id},
            )

    return SessionStartOutcome(
        session_id=session_id,
        started_at=started_at,
        ipc_endpoint=state.ipc_endpoint or {},
    )


async def _start_orchestrator(
    *,
    state: ServerState,
    project_path: Path,
    cfg: RuntimeConfig,
    session_id: str,
    notify: Callable[[JsonRpcNotification], None] | None,
    first_snapshot_timeout_seconds: float,
    seed_path: str | None = None,
) -> None:
    """Boot the AgentShore Orchestrator and supervise its run loop.

    All heavy imports (Orchestrator, StateWriter, IpcStateProvider) are
    deferred to this call so importing ``agentshore.sidecar.*`` at sidecar
    cold start does not transitively load torch (see
    ``tests/sidecar/test_cold_start_torch_free.py``).

    Side effects:
      * Constructs a :class:`agentshore.ipc.state_writer.StateWriter` against
        the same session_dir the EmbeddedBridge is tailing, wraps it in
        :class:`agentshore.ipc.provider.IpcStateProvider`, and passes that as
        the orchestrator's state_provider.
      * Bootstraps the orchestrator (PPO selector loads here — torch
        enters sys.modules at this point, satisfying the §8 cold-start
        invariant which only bans torch from sidecar *startup*).
      * Awaits ``publish_initial_state`` so the dashboard receives state
        before ``first_snapshot`` is marked ok.
      * Registers a natural-exit hook that emits ``session.completed``
        with the same payload shape ``session.stop`` returns.
      * Schedules ``run_until_idle`` as a supervised task on the current
        event loop. The handle is stashed on ``state.orchestrator_task``
        for ``session.stop`` drain/hard logic.
      * Populates ``state.session_context`` with the SessionContext the
        ESR path needs (DataStore handle, archive_path, report_path).
    """
    from agentshore.core import Orchestrator
    from agentshore.ipc.provider import IpcStateProvider
    from agentshore.ipc.state_writer import StateWriter
    from agentshore.session_path import session_dir as _session_dir
    from agentshore.sidecar.notification_emitters import build_session_completed_emitter
    from agentshore.sidecar.server import SessionContext

    sdir = _session_dir(project_path)
    sdir.mkdir(parents=True, exist_ok=True)
    writer = StateWriter(sdir)
    provider = IpcStateProvider(writer)

    orch = await Orchestrator.bootstrap(
        cfg=cfg,
        repo_root=project_path,
        state_provider=provider,
        session_id=session_id,
        # #5: forward the wizard-selected seed file so a desktop-launched
        # session takes the seed bootstrap path instead of silently falling
        # back to open-start (no_seed_input). None ⇒ open-start, as before.
        seed_path=Path(seed_path) if seed_path else None,
        # Issue #561: tell the engine it's hosted inside the desktop sidecar
        # so drain.py skips ``webbrowser.open`` and instead fires the
        # esr_ready callback wired below.
        embedded_mode=True,
    )
    state.orchestrator = orch

    # Wire the esr_ready emitter so drain.py can flag the desktop shell the
    # moment the static ESR HTML lands, replacing the OS-browser handoff
    # (issue #561). Done immediately after bootstrap and before
    # ``publish_initial_state`` so even a fast-failing first snapshot still
    # gets a clean teardown path.
    if notify is not None:
        from agentshore.sidecar.notification_emitters import build_esr_ready_emitter

        emit_esr_ready = build_esr_ready_emitter(notify)

        def _record_and_emit_esr_ready(
            ready_session_id: str,
            report_path: str,
            log_path: str | None,
        ) -> None:
            archive_path = str(project_path / ".agentshore" / "archives" / ready_session_id)
            if ready_session_id == session_id:
                state.esr_ready_report_path = report_path
                state.esr_ready_log_path = log_path
            if (
                state.session_context is not None
                and state.session_context.session_id == ready_session_id
            ):
                state.session_context.report_path = report_path
                state.session_context.log_path = log_path
            emit_esr_ready(ready_session_id, archive_path, report_path, log_path)

        orch.register_esr_ready_callback(_record_and_emit_esr_ready)

    # Wait for the first state snapshot to land in dashboard_state.json so
    # the bridge has something to fan out before the start-checklist's
    # final phase is marked ok.
    try:
        await asyncio.wait_for(orch.publish_initial_state(), timeout=first_snapshot_timeout_seconds)
    except TimeoutError as exc:
        msg = (
            f"first state publish exceeded {first_snapshot_timeout_seconds}s — "
            "orchestrator bootstrap stalled"
        )
        # Tear down the partially-booted orchestrator before propagating so
        # the session is not left half-up.
        with contextlib.suppress(Exception):
            await orch.stop()
        state.orchestrator = None
        raise SessionStartError(STEP_FIRST_SNAPSHOT, -32603, msg) from exc

    # Build a SessionContext now so session.stop's ESR path works. The
    # report/log locators come from the core process via the esr_ready
    # callback; until then report_path stays empty rather than inventing a
    # placeholder path.
    archive_dir = project_path / ".agentshore" / "archives" / session_id
    log_path = getattr(orch, "_log_path", None)
    state.session_context = SessionContext(
        session_id=session_id,
        store=orch._store,
        archive_path=str(archive_dir),
        report_path=state.esr_ready_report_path or "",
        log_path=state.esr_ready_log_path or (str(log_path) if log_path is not None else None),
    )

    # Wire the natural-exit hook to fire session.completed. The hook
    # itself only forms the JSON-RPC notification; the full ESR payload
    # is built by the orchestrator-supervisor task after stop() finishes.
    emit_session_completed = build_session_completed_emitter(notify) if notify is not None else None

    async def _on_natural_exit(reason: str) -> None:
        # The supervisor task below assembles the ESR payload and emits
        # the notification once stop() returns; this hook only records
        # the exit reason so session.stop's mode handling can short-circuit.
        _logger.info(
            "sidecar_orchestrator_natural_exit",
            session_id=session_id,
            reason=reason,
        )

    orch.on_natural_exit(_on_natural_exit)

    async def _supervise() -> None:
        try:
            await orch.run_until_idle()
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("sidecar_orchestrator_run_failed", session_id=session_id)
            raise
        # Only the natural-exit path proceeds to ESR emission; explicit
        # session.stop callers own their own ESR build in the stop path.
        if orch._natural_exit_reason is None:
            return
        # Build the ESR payload BEFORE ``orch.stop()`` — ``stop()`` closes
        # the underlying DataStore as part of shutdown_step:store_close
        # (DESIGN §5.2), and ``ReportDataCollector.collect_end_session_report``
        # then fails with "Session not found" because the SQLite handle is
        # already torn down. Reversing the order keeps the read path
        # talking to a live store and only tears down after we have the
        # snapshot.
        payload: dict[str, object] | None = None
        if emit_session_completed is not None and state.session_context is not None:
            try:
                from agentshore.sidecar.esr import build_esr_payload

                payload = dict(
                    await build_esr_payload(
                        state.session_context.store,
                        state.session_context.session_id,
                        archive_path=state.session_context.archive_path,
                        report_path=state.session_context.report_path,
                        log_path=state.session_context.log_path,
                        exit_reason=orch._natural_exit_reason,
                        exit_code=0,
                    )
                )
            except Exception:
                _logger.exception(
                    "sidecar_session_completed_payload_failed",
                    session_id=session_id,
                )
                # Fall through with a minimal payload so the dashboard
                # still learns the session ended even when the collector
                # could not produce a full summary.
                payload = {
                    "session_id": state.session_context.session_id,
                    "exit_reason": orch._natural_exit_reason,
                    "exit_code": 0,
                    "archive_path": state.session_context.archive_path,
                    "report_path": state.session_context.report_path,
                    "log_path": state.session_context.log_path,
                }
        try:
            await orch.stop()
        except Exception:
            _logger.exception("sidecar_orchestrator_stop_failed", session_id=session_id)
        if payload is not None and state.session_context is not None:
            payload["report_path"] = state.session_context.report_path
            payload["log_path"] = state.session_context.log_path
        if emit_session_completed is not None and payload is not None:
            emit_session_completed(payload)

    def _on_orchestrator_done(task: asyncio.Task[None]) -> None:
        """Retrieve + report a crashed orchestrator task and finalize the session.

        ``_supervise`` is fire-and-forget; without this callback a crash logged
        ``sidecar_orchestrator_run_failed`` and then vanished (the exception was
        never retrieved, and the session row stayed ``running`` forever). The
        callback retrieves the exception (clearing the "never retrieved"
        warning), logs it with a rendered traceback, and writes a terminal
        ``failed`` status off the loop.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        _logger.error(
            "sidecar_orchestrator_task_crashed",
            session_id=session_id,
            exc_info=exc,
        )

        async def _finalize_crashed_session() -> None:
            try:
                await orch._store.fail_session(session_id, "orchestrator_task_crashed")
            except Exception:
                _logger.exception("sidecar_fail_session_failed", session_id=session_id)

        asyncio.get_event_loop().create_task(
            _finalize_crashed_session(), name=f"fail-session-{session_id}"
        )

    state.orchestrator_task = asyncio.create_task(_supervise(), name=f"orchestrator-{session_id}")
    state.orchestrator_task.add_done_callback(_on_orchestrator_done)


def _make_bridge(project_path: Path, ipc_endpoint: dict[str, object]) -> EmbeddedBridge:
    """Construct an EmbeddedBridge bound to the session's IPC endpoint.

    The bridge's WebSocket listener must bind to the same host/port that
    ``session.start`` advertised back to the shell — otherwise the
    desktop dials a port nothing is listening on. Pass them through
    explicitly instead of letting ``EmbeddedBridge`` re-roll a free port.
    """
    endpoint_kind = ipc_endpoint.get("kind")
    if endpoint_kind == "tcp":
        port = ipc_endpoint.get("port")
        host = ipc_endpoint.get("host", "127.0.0.1")
        if not isinstance(port, int) or not isinstance(host, str):
            msg = f"ipc_endpoint missing host/port: {ipc_endpoint}"
            raise SessionStartError(STEP_START_BRIDGE, -32603, msg)
        ipc = IpcEndpoint.tcp(host, port)
    else:
        msg = f"unsupported ipc_endpoint kind for bridge: {endpoint_kind!r}"
        raise SessionStartError(STEP_START_BRIDGE, -32603, msg)
    sdir = session_dir(project_path)
    sdir.mkdir(parents=True, exist_ok=True)
    return EmbeddedBridge(ipc, session_dir=sdir, host=host, port=port)
