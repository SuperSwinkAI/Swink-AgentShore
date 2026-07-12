"""Real ``session.start`` preparation sequence.

DESIGN §10.2 specifies seven canonical bringup phases that the desktop
Screen 8 checklist mirrors:

1. ``config_merge`` — load ``agentshore.yaml`` for the active project.
2. ``check_agent_auth`` — probe each configured CLI agent's backend auth.
3. ``install_skills`` — ensure project-level skill templates are current.
4. ``init_beads`` — ensure the beads graph is initialised.
5. ``bind_ipc`` — reserve a TCP loopback endpoint for the orchestrator.
6. ``start_bridge`` — boot the WebSocket dashboard bridge.
7. ``first_snapshot`` — wait until state can flow to the dashboard.

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
    find_ipc_tcp_port,
    session_dir,
    write_pid,
    write_session_info,
)
from agentshore.sidecar.embedded_bridge import EmbeddedBridge

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Protocol

    from agentshore.config import RuntimeConfig
    from agentshore.data.store import DataStore
    from agentshore.sidecar.server import JsonRpcNotification, ServerState
    from agentshore.state import AgentType

    class OrchestratorHandle(Protocol):
        """Structural view of the orchestrator surface the sidecar drives.

        Declared under ``TYPE_CHECKING`` only so importing
        ``agentshore.sidecar.*`` never transitively loads
        :mod:`agentshore.core` (and therefore torch) at sidecar cold start —
        the invariant pinned by ``tests/sidecar/test_cold_start_torch_free.py``.
        The concrete :class:`agentshore.core.Orchestrator` satisfies this
        protocol structurally; typing ``ServerState.orchestrator`` as
        ``OrchestratorHandle | None`` lets the sidecar drop the
        ``# type: ignore[attr-defined]`` duck-typing comments while keeping the
        engine import lazy.

        The underscored members (``_store``, ``_log_path``,
        ``_natural_exit_reason``) mirror the orchestrator's own internal
        accessors; they are declared here so the sidecar's reach-ins are typed
        rather than silently ``object``-typed.
        """

        def request_drain(self, reason: str = ...) -> None: ...

        def register_esr_ready_callback(
            self, callback: Callable[[str, str, str | None], None] | None
        ) -> None: ...

        def register_session_draining_callback(
            self, callback: Callable[[str, str], None] | None
        ) -> None: ...

        def on_natural_exit(self, callback: Callable[[str], Awaitable[None]]) -> None: ...

        async def set_budget(
            self,
            *,
            dollars_enabled: bool,
            dollars: float | None,
            time_enabled: bool,
            time_minutes: int | None,
            persist: bool = ...,
        ) -> dict[str, object]: ...

        async def current_budget(self) -> dict[str, object]: ...

        async def reload_config(self) -> None: ...

        async def stop(self, grace_period_s: float = ...) -> None: ...

        async def publish_initial_state(self) -> object: ...

        async def run_until_idle(self) -> None: ...

        @property
        def _store(self) -> DataStore: ...

        @property
        def _log_path(self) -> Path | None: ...

        @property
        def _natural_exit_reason(self) -> str | None: ...


_logger = structlog.get_logger(__name__)

# Cap on how long ``first_snapshot`` waits for the orchestrator to publish
# its first state. ``Orchestrator.publish_initial_state`` runs in the boot
# task and completes immediately for empty projects; longer waits indicate
# a stuck bootstrap, so we surface as a phase failure rather than block
# session.start indefinitely.
DEFAULT_FIRST_SNAPSHOT_TIMEOUT_SECONDS: float = 30.0

# Cap on how long a new session.start waits for the *previous* orchestrator's
# store teardown (``orch.stop()`` → ``DataStore.close()``) to finish before it
# boots its own orchestrator (#283). The close does an Online-Backup snapshot +
# ``os.replace`` of agentshore.db and holds the SQLite writer/checkpoint lock;
# running a new ``store_init`` concurrently with it yields "database is locked".
# The window is normally sub-second, but a large DB on throttled disk I/O can
# legitimately take longer, so the bound is generous — exceeding it surfaces a
# clean "still shutting down, retry" error instead of a raw lock failure.
STORE_TEARDOWN_WAIT_SECONDS: float = 60.0


# Canonical step IDs must stay in lockstep with
# ``desktop/src/startupSteps.ts:STARTUP_STEP_IDS``.
STEP_CONFIG_MERGE = "config_merge"
STEP_CHECK_AGENT_AUTH = "check_agent_auth"
STEP_INSTALL_SKILLS = "install_skills"
STEP_INIT_BEADS = "init_beads"
STEP_BIND_IPC = "bind_ipc"
STEP_START_BRIDGE = "start_bridge"
STEP_FIRST_SNAPSHOT = "first_snapshot"

SESSION_START_STEP_IDS: tuple[str, ...] = (
    STEP_CONFIG_MERGE,
    STEP_CHECK_AGENT_AUTH,
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
    STEP_CHECK_AGENT_AUTH: "Agent auth checked",
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
    from agentshore.sidecar.server import notification

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
    return notification("$/progress", params)


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


def _check_config_merge(project_path: Path, *, require_tier_coverage: bool = True) -> RuntimeConfig:
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
        cfg = load_config(config_path)
    except ConfigError as exc:
        raise SessionStartError(STEP_CONFIG_MERGE, -32602, str(exc)) from exc
    if require_tier_coverage:
        from agentshore.agents.model_tiers import REQUIRED_MODEL_TIERS, missing_required_model_tiers

        missing = missing_required_model_tiers(cfg.agents)
        if missing:
            missing_text = ", ".join(missing)
            required = ", ".join(REQUIRED_MODEL_TIERS)
            msg = (
                f"missing required model tier coverage: {missing_text}. "
                "AgentShore start requires at least one enabled, startable agent config "
                f"for each model tier: {required}. Configure agents.<type>.model_tiers "
                "in agentshore.yaml, or rerun agent setup to generate tiered agent configuration."
            )
            raise SessionStartError(STEP_CONFIG_MERGE, -32602, msg)
    return cfg


def _check_agent_auth(cfg: RuntimeConfig) -> None:
    """Validate agent identity and backend auth before the desktop starts.

    This mirrors the CLI start path's identity guard: enabled CLI agents must
    resolve to at least two distinct GitHub logins so review / merge can satisfy
    anti-confirmation-bias constraints, and every identity must cover all model
    tiers (small/medium/large) — code_review is large-tier-only, so concentrating
    ``large`` on one identity deadlocks review (no distinct-identity reviewer).
    After that, probe each configured CLI
    agent's backend auth. The backend probe is blocking in nature
    (``probe_configured_cli_auth`` shells out via ``subprocess.run``); call
    from the async runner through ``asyncio.to_thread``.

    A definitively-expired backend session (e.g. the Codex CLI's cached
    ``chatgpt.com`` token) raises a structured :class:`SessionStartError` so the
    desktop blocks the launch and points at the failing agent type(s) —
    otherwise that agent would hang every dispatch to the idle timeout mid-run.
    Non-blocking non-ok statuses (timeout / probe error / unprobeable) are
    logged and tolerated so a transient probe hiccup never strands an
    otherwise-fine session.
    """
    from agentshore.agents.auth_probe import probe_configured_cli_auth
    from agentshore.agents.identity import (
        require_per_identity_model_tier_coverage,
        require_two_distinct_gh_identities,
    )
    from agentshore.errors import ConfigError

    try:
        require_two_distinct_gh_identities(cfg)
        require_per_identity_model_tier_coverage(cfg)
    except ConfigError as exc:
        raise SessionStartError(STEP_CHECK_AGENT_AUTH, -32603, str(exc)) from exc

    results = probe_configured_cli_auth(cfg)
    blocking = [r for r in results if r.blocks_launch]
    if blocking:
        names = ", ".join(sorted(r.agent_type.value for r in blocking))
        details = "; ".join(f"{r.agent_type.value}: {r.detail}" for r in blocking)
        msg = (
            f"backend auth expired for: {names}. Re-authenticate the agent CLI "
            f"(e.g. run `codex login`) and retry. Details: {details}"
        )
        raise SessionStartError(STEP_CHECK_AGENT_AUTH, -32603, msg)
    for result in results:
        if not result.ok:
            _logger.warning(
                "agent_auth_probe_non_blocking",
                agent_type=result.agent_type.value,
                status=result.status,
                detail=result.detail,
            )


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
    """Pick a free loopback TCP port for the orchestrator IPC channel.

    Uses the stable app-range finder, not an ephemeral port: on Windows the
    ephemeral range is camped by loopback-proxying AV (Avast), which makes a
    pre-resolved ephemeral bind crash the IPC server with WinError 10013. See
    :func:`agentshore.session_path.find_ipc_tcp_port`.
    """
    port = find_ipc_tcp_port("127.0.0.1")
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
    timelapse_enabled: bool | None = None,
) -> SessionStartOutcome:
    """Execute the seven-phase ``session.start`` bringup (async).

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
    # Always mint a fresh id (#283): the sidecar ``session.start`` has no
    # resume-by-id semantics, and reusing a stale ``state.session_id`` left by a
    # prior (naturally ended) session collides with that session's persisted
    # ``sessions`` row → "UNIQUE constraint failed: sessions.session_id" on
    # restart. ``state.session_id`` is an output of start, never an input.
    session_id = str(uuid.uuid4())
    state.session_id = session_id

    # Phase 1: config_merge
    _emit(notify, progress_token, STEP_CONFIG_MERGE, "running")
    cfg: RuntimeConfig | None = None
    if project_path is not None:
        try:
            cfg = _check_config_merge(project_path, require_tier_coverage=start_orchestrator)
        except SessionStartError as exc:
            _emit(notify, progress_token, exc.step, "failed", error=str(exc))
            raise
    _emit(notify, progress_token, STEP_CONFIG_MERGE, "ok")

    # Phase 2: check_agent_auth — probe each configured CLI agent's backend
    # auth (e.g. the Codex CLI's cached chatgpt.com session) now that cfg is
    # resolved but before anything expensive boots. A definitively-expired
    # session blocks the launch with a remediation message; transient probe
    # failures are logged and tolerated. Skipped in legacy stub mode (no
    # active project / cfg).
    _emit(notify, progress_token, STEP_CHECK_AGENT_AUTH, "running")
    if project_path is not None and cfg is not None:
        try:
            await asyncio.to_thread(_check_agent_auth, cfg)
        except SessionStartError as exc:
            _emit(notify, progress_token, exc.step, "failed", error=str(exc))
            raise
    _emit(notify, progress_token, STEP_CHECK_AGENT_AUTH, "ok")

    # Phase 3: install_skills — copy bundled skill templates into
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

    # Phase 4: init_beads
    _emit(notify, progress_token, STEP_INIT_BEADS, "running")
    if project_path is not None:
        try:
            await _run_init_beads(project_path, cfg=cfg)
        except SessionStartError as exc:
            _emit(notify, progress_token, exc.step, "failed", error=str(exc))
            raise
    _emit(notify, progress_token, STEP_INIT_BEADS, "ok")

    # Phase 5: bind_ipc — always allocates a fresh endpoint, even without
    # an active project.
    _emit(notify, progress_token, STEP_BIND_IPC, "running")
    if state.ipc_endpoint is None:
        state.ipc_endpoint = _allocate_ipc_endpoint()
    _emit(notify, progress_token, STEP_BIND_IPC, "ok")

    # Phase 6: start_bridge — boot the EmbeddedBridge as a supervised
    # asyncio task. The bridge serves empty state until the orchestrator
    # publishes to the IPC endpoint allocated above. Failure here (e.g.
    # port collision, missing static bundle) surfaces as a structured
    # ``failed`` $/progress event followed by a SessionStartError.
    _emit(notify, progress_token, STEP_START_BRIDGE, "running")
    if start_bridge and project_path is not None and state.bridge is None:
        try:
            from agentshore.ipc.state_writer import reset_session_files

            # Reset before the bridge primes. This stable session_dir may hold a
            # prior session's state/event files, which the new bridge would
            # otherwise prime before the orchestrator boots (phase 6) and resets
            # them — the same prime-before-reset race as the CLI. The Tier 0
            # session_id gate already rejects a stale snapshot; this makes the
            # embedded path's ordering match the CLI's reset-before-prime.
            reset_session_files(session_dir(project_path))
            bridge = _make_bridge(project_path, state.ipc_endpoint, session_id=session_id)
            await bridge.start()
        except Exception as exc:
            msg = f"failed to start dashboard bridge: {exc}"
            _emit(notify, progress_token, STEP_START_BRIDGE, "failed", error=msg)
            raise SessionStartError(STEP_START_BRIDGE, -32603, msg) from exc
        state.bridge = bridge
    _emit(notify, progress_token, STEP_START_BRIDGE, "ok")

    # Optional: start a dashboard timelapse capture for this session. The
    # per-session ``timelapse_enabled`` override (from the desktop Start
    # toggle) wins over ``cfg.timelapse.enabled``. Best-effort — any failure
    # is logged and swallowed so it can never block session start.
    if start_bridge and project_path is not None and state.bridge is not None:
        effective = (
            timelapse_enabled
            if timelapse_enabled is not None
            else (cfg.timelapse.enabled if cfg is not None else False)
        )
        if effective:
            await _maybe_start_timelapse(state, project_path)

    # Phase 7: first_snapshot — when the orchestrator is enabled, this
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


async def stop_orchestrator_tracked(state: ServerState, orch: OrchestratorHandle) -> None:
    """Run ``orch.stop()`` as a tracked teardown task (#283).

    ``orch.stop()`` closes the orchestrator's ``DataStore`` (Online-Backup
    snapshot + ``os.replace`` of agentshore.db), holding the SQLite
    writer/checkpoint lock for the duration. Publishing the in-flight stop on
    ``state.store_teardown_task`` lets a concurrent ``session.start`` await it
    via :func:`_await_prior_store_teardown` before opening its own store, so the
    two never contend for the lock. The task self-clears the slot on completion
    (only when it is still the registered task, so a fast restart that has
    already registered its own teardown is not clobbered).
    """
    task = asyncio.ensure_future(orch.stop())
    state.store_teardown_task = task
    try:
        await task
    finally:
        if state.store_teardown_task is task:
            state.store_teardown_task = None


async def _await_prior_store_teardown(state: ServerState) -> None:
    """Block until any in-flight orchestrator store teardown finishes (#283).

    Returns immediately when no teardown is running. Uses ``shield`` so a
    timeout here never cancels the actual close (which must run to completion to
    leave agentshore.db consistent); on timeout it raises a clean
    ``SessionStartError`` rather than letting the new ``store_init`` race the
    close and fail with a bare "database is locked".
    """
    task = state.store_teardown_task
    if task is None or task.done():
        return
    _logger.info("session_start_awaiting_store_teardown")
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=STORE_TEARDOWN_WAIT_SECONDS)
    except TimeoutError as exc:
        raise SessionStartError(
            STEP_FIRST_SNAPSHOT,
            -32603,
            "previous session is still shutting down (database close in progress) — "
            "retry in a moment",
        ) from exc


def _clear_session_state(state: ServerState) -> None:
    """Reset per-session ``ServerState`` after a session ends (#283).

    The explicit ``session.stop`` path clears these inline
    (``handlers/session.py``); the natural-exit (self-drain → shutdown_complete)
    and crash paths must do the same. Otherwise, in the long-lived sidecar, the
    stale ``session_id`` is reused by the next ``session.start`` — colliding with
    its persisted ``sessions`` row (UNIQUE constraint) — and the dead
    orchestrator + its (closed) store linger reachable instead of being GC'd.
    Does not touch the dashboard bridge: the desktop still serves the ESR after a
    natural end, so bridge teardown stays owned by the explicit-stop path.
    """
    state.session_active = False
    state.session_id = None
    state.started_at = None
    state.session_context = None
    state.esr_ready_report_path = None
    state.esr_ready_log_path = None
    state.orchestrator = None
    state.orchestrator_task = None


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

    # #283: a previous session's store teardown (orch.stop() → DataStore.close,
    # which snapshots + os.replaces agentshore.db under the writer lock) may
    # still be running — the ESR "restart" button unlocks before that close
    # finishes. Wait for it to complete before opening this session's store, or
    # the new store_init contends with the close and hits "database is locked".
    await _await_prior_store_teardown(state)

    sdir = _session_dir(project_path)
    sdir.mkdir(parents=True, exist_ok=True)
    writer = StateWriter(sdir)
    provider = IpcStateProvider(writer, session_id=session_id)

    orch = await Orchestrator.bootstrap(
        cfg=cfg,
        repo_root=project_path,
        state_provider=provider,
        session_id=session_id,
        # #5: forward the wizard-selected seed file so a desktop-launched
        # session takes the seed bootstrap path instead of silently falling
        # back to open-start (no_seed_input). None ⇒ open-start, as before.
        seed_path=Path(seed_path) if seed_path else None,
        # Persist live budget changes (and SIGHUP reloads) to the project's
        # agentshore.yaml — without this the orchestrator's _config_path stays
        # None and Orchestrator.set_budget(persist=True) silently no-ops, so a
        # desktop "Adjust Budget…" change would not survive a restart.
        config_path=project_path / "agentshore.yaml",
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

    # Wire the session.draining emitter so the Tauri shell's heartbeat
    # watchdog can stand down the moment graceful shutdown begins, rather
    # than waiting for $/esr_ready (which only arrives after the unbounded
    # ESR HTML-generation step completes).
    if notify is not None:
        from agentshore.sidecar.notification_emitters import build_session_draining_emitter

        emit_session_draining = build_session_draining_emitter(notify)
        orch.register_session_draining_callback(emit_session_draining)

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
    log_path = orch._log_path
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
        # Snapshot both locals immediately — a concurrent _clear_session_state
        # (or the explicit-stop path) can null state.session_context during the
        # run_until_idle/teardown window. Reading state.session_context live
        # across awaits is the race that silently skips the rich payload and
        # leaves only a bare $/esr_ready on natural exit. Mirror the robust
        # explicit-stop snapshot in rpc/handlers/session.py:117.
        ctx = state.session_context
        reason = orch._natural_exit_reason
        # Build the ESR payload BEFORE ``orch.stop()`` — ``stop()`` closes
        # the underlying DataStore as part of shutdown_step:store_close
        # (DESIGN §5.2), and ``ReportDataCollector.collect_end_session_report``
        # then fails with "Session not found" because the SQLite handle is
        # already torn down. Reversing the order keeps the read path
        # talking to a live store and only tears down after we have the
        # snapshot.
        payload: dict[str, object] | None = None
        if emit_session_completed is not None and ctx is not None:
            try:
                from agentshore.sidecar.esr import build_esr_payload

                payload = dict(
                    await build_esr_payload(
                        ctx.store,
                        ctx.session_id,
                        archive_path=ctx.archive_path,
                        report_path=ctx.report_path,
                        log_path=ctx.log_path,
                        exit_reason=reason,
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
                    "session_id": ctx.session_id,
                    "exit_reason": reason,
                    "exit_code": 0,
                    "archive_path": ctx.archive_path,
                    "report_path": ctx.report_path,
                    "log_path": ctx.log_path,
                }
        try:
            await stop_orchestrator_tracked(state, orch)
        except Exception:
            _logger.exception("sidecar_orchestrator_stop_failed", session_id=session_id)
        if payload is not None and ctx is not None:
            payload["report_path"] = ctx.report_path
            payload["log_path"] = ctx.log_path
        # Stop any timelapse capture (best-effort) and attach the MP4 path so
        # the desktop opens it when the naturally-ended session completes.
        if payload is not None:
            payload["timelapse_output_path"] = await stop_timelapse_capture(state)
        if emit_session_completed is not None and payload is not None:
            emit_session_completed(payload)
        # Natural exit closed the store above (stop_orchestrator_tracked) but, unlike
        # the explicit-stop path, never cleared session state — leaving the stale
        # session_id to be reused on the next start (#283). Clear it now.
        _clear_session_state(state)

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
            # A crashed run never reached stop_orchestrator_tracked in _supervise,
            # so the store is still open. Close it (+ clear state) or the
            # connection leaks into the long-lived sidecar and blocks the next
            # session.start (#283). orch.stop() is re-entrancy safe.
            try:
                await stop_orchestrator_tracked(state, orch)
            except Exception:
                _logger.exception("sidecar_crashed_stop_failed", session_id=session_id)
            _clear_session_state(state)

        asyncio.get_running_loop().create_task(
            _finalize_crashed_session(), name=f"fail-session-{session_id}"
        )

    state.orchestrator_task = asyncio.create_task(_supervise(), name=f"orchestrator-{session_id}")
    state.orchestrator_task.add_done_callback(_on_orchestrator_done)


def _timelapse_runs_cwd(project_path: Path) -> Path:
    """Working dir for all timelapse calls (so the run-id resolves consistently)."""
    return project_path / ".agentshore"


def _dashboard_url(ipc_endpoint: dict[str, object] | None) -> str | None:
    """Build the dashboard HTTP root the bridge serves, from the IPC endpoint."""
    if not isinstance(ipc_endpoint, dict):
        return None
    host = ipc_endpoint.get("host")
    port = ipc_endpoint.get("port")
    if not isinstance(host, str) or not isinstance(port, int):
        return None
    return f"http://{host}:{port}/"


async def _maybe_start_timelapse(state: ServerState, project_path: Path) -> None:
    """Start a best-effort dashboard timelapse; stash the run-id on *state*."""
    url = _dashboard_url(state.ipc_endpoint)
    if url is None:
        _logger.warning("timelapse_start_skipped", reason="no_dashboard_url")
        return
    from agentshore.timelapse import TimelapseError, start_capture

    runs_cwd = _timelapse_runs_cwd(project_path)
    try:
        run = await start_capture(url, runs_cwd)
    except TimelapseError as exc:
        _logger.warning("timelapse_start_failed", error=str(exc))
        return
    state.timelapse_run_id = run.run_id
    state.timelapse_runs_cwd = runs_cwd


async def stop_timelapse_capture(state: ServerState) -> str | None:
    """Stop any active capture and return the rendered MP4 path (best-effort).

    Clears the run-id from *state*. Never raises — a stuck or failed render
    must not wedge session shutdown. Returns None when no capture was running
    or the render did not complete in time.
    """
    run_id = state.timelapse_run_id
    runs_cwd = state.timelapse_runs_cwd
    state.timelapse_run_id = None
    if run_id is None or runs_cwd is None:
        return None
    from agentshore.timelapse import TimelapseError, await_output, stop_capture

    try:
        await stop_capture(run_id, runs_cwd)
    except TimelapseError as exc:
        _logger.warning("timelapse_stop_failed", run_id=run_id, error=str(exc))
        return None
    return await await_output(run_id, runs_cwd)


def _make_bridge(
    project_path: Path,
    ipc_endpoint: dict[str, object],
    *,
    session_id: str | None = None,
) -> EmbeddedBridge:
    """Construct an EmbeddedBridge bound to the session's IPC endpoint.

    The bridge's WebSocket listener must bind to the same host/port that
    ``session.start`` advertised back to the shell — otherwise the
    desktop dials a port nothing is listening on. Pass them through
    explicitly instead of letting ``EmbeddedBridge`` re-roll a free port.

    ``session_id`` pins the bridge's session identity before the orchestrator
    boots so it rejects any prior session's stale on-disk snapshot.
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
    return EmbeddedBridge(ipc, session_dir=sdir, host=host, port=port, session_id=session_id)
