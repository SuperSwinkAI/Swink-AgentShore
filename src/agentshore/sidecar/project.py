"""project.* RPC method implementations (DESIGN §5.1).

The sidecar maintains a single ``ActiveProject`` slot (§1.3). ``project.select``
sets it, ``project.deselect`` clears it, and ``inspect``/``branches``/
``set_target_branch`` operate against it. Switching projects while a session
is running is the caller's responsibility to gate (§1.3: ``ERR_SESSION_ACTIVE``).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from agentshore import subprocess_env
from agentshore.beads import resolve_bd_binary
from agentshore.budget import (
    MAX_TIME_BUDGET_MINUTES,
    MIN_ENABLED_BUDGET_USD,
    MIN_TIME_BUDGET_MINUTES,
)
from agentshore.command import CommandResult, git_sync
from agentshore.config.models import BudgetConfig, TimelapseConfig

# Internal project.* error codes (mapped to public codes by the dispatcher).
# ERR_PROJECT_NOT_ACTIVE is remapped to server.ERR_NO_ACTIVE_PROJECT (-32011)
# for project.{inspect,branches,set_target_branch}; see server._dispatch.
ERR_PROJECT_NOT_ACTIVE = -32004
GIT_PROBE_TIMEOUT_SECONDS = 5.0
GIT_TIMEOUT_RETURN_CODE = 124
# Deliberately short: inspect() fans probes out concurrently via asyncio.gather
# and returns within this deadline with fallback values for any slow probe, so
# the Readiness screen always paints fast. The git layer's non-interactive env
# (no credential prompt) is what removes the real hang risk — not a longer
# deadline here.
INSPECT_PROBE_TIMEOUT_SECONDS = 2.0
BRANCH_LIST_TIMEOUT_SECONDS = 20.0
BRANCH_REMOTE_ONLY_LIMIT = 200

# Dedicated thread pool for inspect() probes. Using a custom executor (not the
# asyncio default executor) means asyncio.run() / loop.shutdown_default_executor()
# does not wait for slow probe threads — consistent with the original
# concurrent.futures-based implementation.
_PROBE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=16,
    thread_name_prefix="agentshore-project-probe",
)


class ProjectError(Exception):
    """Raised for project.* operation failures; mapped to JSON-RPC errors.

    ``code`` is the JSON-RPC error code reported back to the shell. Values
    below -32000 are reserved for sidecar-defined application errors.
    """

    def __init__(self, message: str, code: int = -32000) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ActiveProject:
    path: Path


def _resolve_path(path: str) -> Path:
    if not isinstance(path, str) or not path:
        raise ProjectError("path must be a non-empty string")
    expanded = Path(path).expanduser()
    resolved = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if not resolved.is_dir():
        raise ProjectError(f"path is not a directory: {resolved}", code=-32001)
    return resolved


class _ProjectState:
    """Module-private holder for the single active-project slot."""

    __slots__ = ("_active",)

    def __init__(self) -> None:
        self._active: ActiveProject | None = None

    def current(self) -> ActiveProject | None:
        return self._active

    def select(self, path: str) -> dict[str, object]:
        resolved = _resolve_path(path)
        self._active = ActiveProject(path=resolved)
        return {"path": str(resolved)}

    def deselect(self) -> dict[str, object]:
        self._active = None
        return {}

    def reset_for_tests(self) -> None:
        self._active = None


_state = _ProjectState()


def current() -> ActiveProject | None:
    """Return the active project slot, or None if no project is selected."""
    return _state.current()


def reset_state_for_tests() -> None:
    """Test helper: clear the active project slot."""
    _state.reset_for_tests()


def select(path: str) -> dict[str, object]:
    """Set the active project to *path*. Idempotent.

    Raises ``ProjectError`` if the path does not exist or is not a directory.
    """
    return _state.select(path)


def deselect() -> dict[str, object]:
    """Clear the active project slot. No-op if none is selected."""
    return _state.deselect()


def _require_active() -> Path:
    active = _state.current()
    if active is None:
        raise ProjectError(
            "no active project; call project.select first", code=ERR_PROJECT_NOT_ACTIVE
        )
    return active.path


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    timeout_seconds: float = GIT_PROBE_TIMEOUT_SECONDS,
) -> CommandResult:
    """Run a read-only git probe through the hardened, non-interactive runner.

    Returns a :class:`CommandResult` (returncode ``124`` on timeout, ``127``
    when git is not installed) — the credential-neutralizing env guarantees the
    probe can never hang on a Git-Credential-Manager / askpass dialog.
    """
    return git_sync(*args, cwd=cwd, timeout_seconds=timeout_seconds)


def _repo_identity_probe_fallback(path: Path) -> dict[str, object]:
    return {
        "is_git": True,
        "root": str(path),
        "head_sha": "",
        "origin_url": None,
        "probe_error": "repo identity probe timed out",
    }


def _repo_identity(path: Path) -> dict[str, object]:
    head = _run_git(["rev-parse", "HEAD"], path)
    if head.returncode == GIT_TIMEOUT_RETURN_CODE:
        return {
            "is_git": True,
            "root": str(path),
            "head_sha": "",
            "origin_url": None,
            "probe_error": head.stderr.strip(),
        }
    if head.returncode != 0:
        return {"is_git": False}
    root = _run_git(["rev-parse", "--show-toplevel"], path)
    origin = _run_git(["config", "--get", "remote.origin.url"], path)
    return {
        "is_git": True,
        "root": root.stdout.strip() or str(path),
        "head_sha": head.stdout.strip(),
        "origin_url": origin.stdout.strip() if origin.returncode == 0 else None,
    }


def _current_branch(path: Path) -> str | None:
    out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    if out.returncode != 0:
        return None
    branch = out.stdout.strip()
    return branch if branch and branch != "HEAD" else None


_TOOL_MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "python"),
    ("uv.lock", "uv"),
    ("requirements.txt", "pip"),
    ("package.json", "node"),
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("Gemfile", "ruby"),
    ("Makefile", "make"),
)


def _detected_tools(path: Path) -> list[str]:
    return [tool for marker, tool in _TOOL_MARKERS if (path / marker).exists()]


def _agentshore_yaml_payload(path: Path) -> dict[str, object] | None:
    yaml_path = path / "agentshore.yaml"
    if not yaml_path.is_file():
        return None
    try:
        raw = yaml_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"path": str(yaml_path), "error": str(exc)}
    return {"path": str(yaml_path), "raw": raw}


def _beads_status(path: Path) -> dict[str, object]:
    return {"initialised": (path / ".beads").is_dir()}


def _prerequisites() -> dict[str, object]:
    return {
        "git": subprocess_env.resolve_tool("git") is not None,
        "bd": resolve_bd_binary() is not None,
        "gh": subprocess_env.resolve_tool("gh") is not None,
    }


async def inspect() -> dict[str, object]:
    """Return the inspection envelope for the active project (DESIGN §5.1).

    All blocking git probes run off the event loop via ``loop.run_in_executor``
    using a dedicated ``_PROBE_EXECUTOR`` (not the asyncio default executor, so
    ``asyncio.run`` / ``loop.shutdown_default_executor`` does not wait for slow
    threads). :func:`asyncio.wait` collects whichever probes finish within
    ``INSPECT_PROBE_TIMEOUT_SECONDS``; pending probes get a safe fallback value
    so the Readiness screen always paints fast.
    """
    path = _require_active()
    loop = asyncio.get_event_loop()

    def _run(fn: object) -> asyncio.Task[object]:
        return asyncio.ensure_future(loop.run_in_executor(_PROBE_EXECUTOR, fn))  # type: ignore[arg-type]

    # Build (task, fallback) pairs — each task wraps a blocking git probe.
    probe_pairs: list[tuple[asyncio.Future[object], object]] = [
        (_run(lambda: _repo_identity(path)), _repo_identity_probe_fallback(path)),
        (_run(lambda: _current_branch(path)), None),
        (_run(lambda: _detected_tools(path)), []),
        (_run(lambda: _agentshore_yaml_payload(path)), None),
        (
            _run(lambda: _beads_status(path)),
            {"initialised": False, "probe_error": "beads status probe timed out"},
        ),
        (
            _run(_prerequisites),
            {"git": False, "bd": False, "gh": False, "probe_error": "tooling probe timed out"},
        ),
    ]
    tasks = [t for t, _ in probe_pairs]

    done, _ = await asyncio.wait(tasks, timeout=INSPECT_PROBE_TIMEOUT_SECONDS)

    results: list[object] = []
    for task, fallback in probe_pairs:
        if task in done and not task.cancelled():
            exc = task.exception()
            results.append(fallback if exc is not None else task.result())
        else:
            results.append(fallback)

    repo_identity, branch, detected_tools, agentshore_yaml, beads_status, prerequisites = results

    return {
        "path": str(path),
        "repo_identity": repo_identity,
        "branch": branch,
        "detected_tools": detected_tools,
        "agentshore_yaml": agentshore_yaml,
        "beads_status": beads_status,
        "prerequisites": prerequisites,
    }


def _default_branch_name(path: Path) -> str:
    out = _run_git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path)
    if out.returncode == 0:
        ref = out.stdout.strip()
        if ref.startswith("origin/"):
            return ref[len("origin/") :]
        return ref or "main"
    return "main"


class _BranchRow(TypedDict):
    name: str
    is_default: bool
    is_current: bool
    is_remote: bool
    ahead: int | None
    behind: int | None


def _ahead_behind_sync(
    path: Path, ref: str, target: str, *, timeout_seconds: float
) -> tuple[int, int] | None:
    """Return (ahead, behind) for *ref* vs *target*, or ``None`` on failure."""
    out = _run_git(
        ["rev-list", "--left-right", "--count", f"{ref}...{target}"],
        path,
        timeout_seconds=timeout_seconds,
    )
    if out.returncode != 0:
        return None
    parts = out.stdout.strip().split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _build_branch_rows(
    path: Path,
    *,
    default: str,
    current_branch: str | None,
    local_order: list[str],
    remote_names: list[str],
    deadline: float | None,
) -> list[_BranchRow]:
    """Build branch rows. ``ahead``/``behind`` are ``None`` unless *deadline* is set."""
    import time

    target = f"origin/{default}"
    rows: list[_BranchRow] = []
    local_names = set(local_order)
    for name in local_order:
        ahead: int | None = None
        behind: int | None = None
        if deadline is not None:
            remaining = max(0.1, min(GIT_PROBE_TIMEOUT_SECONDS, deadline - time.monotonic()))
            result = _ahead_behind_sync(path, name, target, timeout_seconds=remaining)
            if result is not None:
                ahead, behind = result
        rows.append(
            {
                "name": name,
                "is_default": name == default,
                "is_current": name == current_branch,
                "is_remote": False,
                "ahead": ahead,
                "behind": behind,
            }
        )
    remote_rows_added = 0
    for name in remote_names:
        if name in local_names:
            continue
        ahead = None
        behind = None
        if deadline is not None:
            remaining = max(0.1, min(GIT_PROBE_TIMEOUT_SECONDS, deadline - time.monotonic()))
            result = _ahead_behind_sync(path, f"origin/{name}", target, timeout_seconds=remaining)
            if result is not None:
                ahead, behind = result
        rows.append(
            {
                "name": name,
                "is_default": name == default,
                "is_current": False,
                "is_remote": True,
                "ahead": ahead,
                "behind": behind,
            }
        )
        remote_rows_added += 1
        if remote_rows_added >= BRANCH_REMOTE_ONLY_LIMIT:
            break
    return rows


def _list_branches_sync(path: Path, deadline: float | None = None) -> list[_BranchRow]:
    """List branches via ``git for-each-ref`` (synchronous; no fetch).

    ``ahead``/``behind`` are always ``None`` on this path — the caller is
    responsible for computing them if they are needed.
    """
    import time

    def _remaining() -> float:
        if deadline is None:
            return GIT_PROBE_TIMEOUT_SECONDS
        return max(0.1, min(GIT_PROBE_TIMEOUT_SECONDS, deadline - time.monotonic()))

    default = _default_branch_name(path)
    current = _current_branch(path)

    local_res = _run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        path,
        timeout_seconds=_remaining(),
    )
    remote_res = _run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"],
        path,
        timeout_seconds=_remaining(),
    )

    local_order: list[str] = []
    remote_names: list[str] = []
    local_seen: set[str] = set()

    if local_res.returncode == 0:
        for line in local_res.stdout.splitlines():
            ref = line.strip()
            if not ref or ref == "origin":
                continue
            if ref not in local_seen:
                local_seen.add(ref)
                local_order.append(ref)

    if remote_res.returncode == 0:
        for line in remote_res.stdout.splitlines():
            ref = line.strip()
            if not ref or not ref.startswith("origin/"):
                continue
            name = ref[len("origin/") :]
            if name and name != "HEAD":
                remote_names.append(name)

    # No deadline passed to _build_branch_rows — ahead/behind stay None
    return _build_branch_rows(
        path,
        default=default,
        current_branch=current,
        local_order=local_order,
        remote_names=remote_names,
        deadline=None,
    )


async def branches(*, refresh: bool = False) -> list[_BranchRow]:
    """Return local + remote-tracking branches for the target-branch picker.

    Both paths use ``git for-each-ref`` through the hardened non-interactive
    runner and run off the event loop via ``asyncio.to_thread``.

    On the default (``refresh=False``) path, ``ahead``/``behind`` are always
    ``None`` — they are never fabricated as ``0``.

    On the ``refresh=True`` path, ``git fetch --prune origin`` is run first,
    then ``ahead``/``behind`` are computed per-branch up to
    ``BRANCH_LIST_TIMEOUT_SECONDS``.
    """
    import time

    path = _require_active()

    if not refresh:
        return await asyncio.to_thread(_list_branches_sync, path)

    deadline = time.monotonic() + BRANCH_LIST_TIMEOUT_SECONDS

    def _refresh_sync() -> list[_BranchRow]:
        remaining = max(0.1, deadline - time.monotonic())
        fetch_res = _run_git(
            ["fetch", "--prune", "origin"],
            path,
            timeout_seconds=remaining,
        )
        if fetch_res.returncode == GIT_TIMEOUT_RETURN_CODE or time.monotonic() >= deadline:
            return _list_branches_sync(path)

        default = _default_branch_name(path)
        current = _current_branch(path)

        def _rem() -> float:
            return max(0.1, min(GIT_PROBE_TIMEOUT_SECONDS, deadline - time.monotonic()))

        local_res = _run_git(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            path,
            timeout_seconds=_rem(),
        )
        if time.monotonic() >= deadline:
            return _list_branches_sync(path)
        remote_res = _run_git(
            ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"],
            path,
            timeout_seconds=_rem(),
        )

        local_seen: set[str] = set()
        local_order: list[str] = []
        remote_names: list[str] = []

        if local_res.returncode == 0:
            for line in local_res.stdout.splitlines():
                ref = line.strip()
                if not ref or ref == "origin":
                    continue
                if ref not in local_seen:
                    local_seen.add(ref)
                    local_order.append(ref)

        if remote_res.returncode == 0:
            for line in remote_res.stdout.splitlines():
                ref = line.strip()
                if not ref or not ref.startswith("origin/"):
                    continue
                name = ref[len("origin/") :]
                if name and name != "HEAD":
                    remote_names.append(name)

        return _build_branch_rows(
            path,
            default=default,
            current_branch=current,
            local_order=local_order,
            remote_names=remote_names,
            deadline=deadline,
        )

    return await asyncio.to_thread(_refresh_sync)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (temp + fsync + rename)."""
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".agentshore_yaml_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def set_target_branch(name: str) -> dict[str, object]:
    """Persist *name* as ``project.target_branch`` in agentshore.yaml (DESIGN §4.1).

    Validates the branch name via ``git check-ref-format`` and that the branch
    exists in local or ``origin/*`` refs via ``git for-each-ref``.
    """
    from agentshore.sidecar.yaml_edits import write_target_branch as _write

    path = _require_active()
    if not isinstance(name, str) or not name.strip():
        raise ProjectError("name must be a non-empty string")
    name = name.strip()

    # Validate branch name via git check-ref-format
    check = _run_git(["check-ref-format", "--branch", name], path)
    if check.returncode != 0:
        raise ProjectError(f"invalid branch name: {name!r}", code=-32002)

    # Validate the branch exists in local or remote refs
    local_res = _run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        path,
    )
    remote_res = _run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"],
        path,
    )
    local_names: set[str] = set()
    remote_names: set[str] = set()
    if local_res.returncode == 0:
        for line in local_res.stdout.splitlines():
            ref = line.strip()
            if ref and ref != "origin":
                local_names.add(ref)
    if remote_res.returncode == 0:
        for line in remote_res.stdout.splitlines():
            ref = line.strip()
            if ref.startswith("origin/"):
                remote_names.add(ref[len("origin/") :])
    if name not in local_names | remote_names:
        raise ProjectError(
            f"branch '{name}' not found in local or origin-tracking refs",
            code=-32002,
        )

    yaml_path = path / "agentshore.yaml"
    try:
        existing = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
        new_text = _write(existing, name)
        _atomic_write_text(yaml_path, new_text)
    except Exception as exc:
        raise ProjectError(f"agentshore.yaml update failed: {exc}", code=-32003) from exc
    return {"target_branch": name, "yaml_path": str(yaml_path)}


def set_seed_paths(payload: object) -> dict[str, object]:
    """Persist seed material paths as ``intake.seed_paths`` in agentshore.yaml.

    Accepts a single path string or a list of strings (relative to the project
    root, or absolute). Each must be a non-empty string and exist on disk. An
    empty list clears the configured seed. Write is atomic (temp + fsync +
    rename). Mirrors :func:`set_target_branch` so any start path — CLI, sidecar,
    desktop Quick Start, TUI — picks up the configured seed via the bootstrap
    ``_resolve_seed_path`` fallback.
    """
    from agentshore.sidecar.yaml_edits import write_seed_paths as _write

    path = _require_active()
    if isinstance(payload, str):
        raw_paths: list[object] = [payload]
    elif isinstance(payload, list):
        raw_paths = payload
    else:
        raise ProjectError("seed_paths must be a string or list of strings")

    seed_paths: list[str] = []
    for entry in raw_paths:
        if not isinstance(entry, str) or not entry.strip():
            raise ProjectError("each seed path must be a non-empty string")
        s = entry.strip()
        candidate = Path(s).expanduser()
        if not candidate.is_absolute():
            candidate = path / candidate
        if not candidate.exists():
            raise ProjectError(f"seed path not found: {s}", code=-32002)
        seed_paths.append(s)

    yaml_path = path / "agentshore.yaml"
    try:
        existing = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
        new_text = _write(existing, seed_paths)
        _atomic_write_text(yaml_path, new_text)
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError(f"agentshore.yaml update failed: {exc}", code=-32003) from exc
    return {"seed_paths": seed_paths, "yaml_path": str(yaml_path)}


# Accepted keys on the ``budget:`` mapping written to agentshore.yaml. Mirrors
# the ``BudgetConfig`` dataclass in ``src/agentshore/config/models.py``.
_BUDGET_KEYS: frozenset[str] = frozenset(
    {"enabled", "total", "warning_threshold", "time_enabled", "time_total_minutes"}
)


def _validate_budget_payload(payload: object) -> BudgetConfig:
    """Validate the ``set_budget`` payload and return a :class:`BudgetConfig`.

    Rules:
    * ``enabled`` (bool, required).
    * ``total`` (number, required, finite, ``>= 0``). NaN/Inf rejected.
      When ``enabled`` is ``True``, ``total`` must be ``>= MIN_ENABLED_BUDGET_USD``
      so the persisted YAML round-trips through ``load_config`` (which enforces
      the same floor).
    * ``warning_threshold`` (number, optional, finite, ``0 <= x <= 1``;
      defaults to ``0.20``).
    * No unknown keys.
    """
    if not isinstance(payload, dict):
        raise ProjectError("budget payload must be an object")
    unknown = set(payload.keys()) - _BUDGET_KEYS
    if unknown:
        raise ProjectError(f"unknown budget fields: {sorted(unknown)}")
    if "enabled" not in payload:
        raise ProjectError("budget.enabled is required")
    enabled = payload["enabled"]
    if not isinstance(enabled, bool):
        raise ProjectError("budget.enabled must be a boolean")
    if "total" not in payload:
        raise ProjectError("budget.total is required")
    total_raw = payload["total"]
    # ``bool`` is a subclass of ``int`` — reject it explicitly so ``True``/
    # ``False`` cannot pose as a dollar amount.
    if isinstance(total_raw, bool) or not isinstance(total_raw, (int, float)):
        raise ProjectError("budget.total must be a number")
    total = float(total_raw)
    if not math.isfinite(total):
        raise ProjectError("budget.total must be finite")
    if total < 0:
        raise ProjectError("budget.total must be >= 0")
    # Mirror ``agentshore.config._parse_budget``: an enabled budget below
    # ``MIN_ENABLED_BUDGET_USD`` is rejected by ``load_config`` on the next
    # round-trip, so we must reject it here too to keep the RPC and the
    # config-loader contract in sync.
    if enabled and total < MIN_ENABLED_BUDGET_USD:
        raise ProjectError(
            "budget.total must be at least "
            f"{MIN_ENABLED_BUDGET_USD:.2f} when budget.enabled is true, got {total!r}"
        )
    threshold = 0.20
    if "warning_threshold" in payload:
        threshold_raw = payload["warning_threshold"]
        if isinstance(threshold_raw, bool) or not isinstance(threshold_raw, (int, float)):
            raise ProjectError("budget.warning_threshold must be a number")
        threshold = float(threshold_raw)
        if not math.isfinite(threshold):
            raise ProjectError("budget.warning_threshold must be finite")
        if threshold < 0 or threshold > 1:
            raise ProjectError("budget.warning_threshold must be between 0 and 1")
    # Time soft cap (independent dimension). Both fields optional; default off.
    time_enabled = payload.get("time_enabled", False)
    if not isinstance(time_enabled, bool):
        raise ProjectError("budget.time_enabled must be a boolean")
    time_total_minutes_raw = payload.get("time_total_minutes", 0)
    if isinstance(time_total_minutes_raw, bool) or not isinstance(time_total_minutes_raw, int):
        raise ProjectError("budget.time_total_minutes must be an integer")
    time_total_minutes = int(time_total_minutes_raw)
    # Mirror ``_parse_budget``: an enabled time cap outside 1h–72h is rejected by
    # ``load_config`` on the next round-trip, so reject it here to stay in sync.
    if time_enabled and not (
        MIN_TIME_BUDGET_MINUTES <= time_total_minutes <= MAX_TIME_BUDGET_MINUTES
    ):
        raise ProjectError(
            f"budget.time_total_minutes must be between {MIN_TIME_BUDGET_MINUTES} and "
            f"{MAX_TIME_BUDGET_MINUTES} (1h–72h) when budget.time_enabled is true, "
            f"got {time_total_minutes!r}"
        )
    if not time_enabled and time_total_minutes < 0:
        raise ProjectError("budget.time_total_minutes must be non-negative")
    return BudgetConfig(
        enabled=enabled,
        total=total,
        warning_threshold=threshold,
        time_enabled=time_enabled,
        time_total_minutes=time_total_minutes,
    )


def set_budget(payload: object) -> dict[str, object]:
    """Persist the ``budget`` block in agentshore.yaml (issue #571 follow-up).

    Validates the incoming payload and atomically rewrites the ``budget:``
    key. Other keys / comments / ordering are preserved via ruamel.yaml.

    The payload shape mirrors :class:`BudgetConfig` in
    ``src/agentshore/config/models.py``::

        {"enabled": bool, "total": float, "warning_threshold": float?,
         "time_enabled": bool?, "time_total_minutes": int?}

    Returns the persisted values (echoing what was written) plus the
    resolved ``yaml_path``.
    """
    from agentshore.sidecar.yaml_edits import write_budget as _write

    path = _require_active()
    budget = _validate_budget_payload(payload)
    yaml_path = path / "agentshore.yaml"
    try:
        existing = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
        new_text = _write(existing, budget)
        _atomic_write_text(yaml_path, new_text)
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError(f"agentshore.yaml update failed: {exc}", code=-32003) from exc
    return {
        "budget": {
            "enabled": budget.enabled,
            "total": budget.total,
            "warning_threshold": budget.warning_threshold,
            "time_enabled": budget.time_enabled,
            "time_total_minutes": budget.time_total_minutes,
        },
        "yaml_path": str(yaml_path),
    }


def set_trusted_issue_enforcement(payload: object) -> dict[str, object]:
    """Persist ``trusted_ids.restrict_issues_to_trusted_authors`` in agentshore.yaml.

    The desktop toggles "only work issues from trusted identities" via this
    method. The dispatcher extracts ``enabled`` and passes the bare bool, so
    *payload* must be a bool. Write is atomic (temp + fsync + rename); other
    keys / comments / ordering are preserved via ruamel.yaml.

    Returns ``{"enabled": bool, "yaml_path": str}``.
    """
    from agentshore.sidecar.yaml_edits import write_trusted_issue_enforcement as _write

    path = _require_active()
    if not isinstance(payload, bool):
        raise ProjectError("enabled must be a boolean")
    enabled = payload
    yaml_path = path / "agentshore.yaml"
    try:
        existing = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
        new_text = _write(existing, enabled)
        _atomic_write_text(yaml_path, new_text)
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError(f"agentshore.yaml update failed: {exc}", code=-32003) from exc
    return {"enabled": enabled, "yaml_path": str(yaml_path)}


# Accepted keys on the ``timelapse:`` mapping written to agentshore.yaml.
# Mirrors the ``TimelapseConfig`` dataclass in
# ``src/agentshore/config/models.py``.
_TIMELAPSE_KEYS: frozenset[str] = frozenset({"enabled", "installed"})


def _validate_timelapse_payload(payload: object) -> TimelapseConfig:
    """Validate the ``set_timelapse`` payload and return a :class:`TimelapseConfig`.

    Both ``enabled`` and ``installed`` are optional booleans (default False);
    no unknown keys are allowed.
    """
    if not isinstance(payload, dict):
        raise ProjectError("timelapse payload must be an object")
    unknown = set(payload.keys()) - _TIMELAPSE_KEYS
    if unknown:
        raise ProjectError(f"unknown timelapse fields: {sorted(unknown)}")
    enabled = payload.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ProjectError("timelapse.enabled must be a boolean")
    installed = payload.get("installed", False)
    if not isinstance(installed, bool):
        raise ProjectError("timelapse.installed must be a boolean")
    return TimelapseConfig(enabled=enabled, installed=installed)


def _persist_timelapse(path: Path, timelapse: TimelapseConfig) -> str:
    """Write the ``timelapse`` block into *path*/agentshore.yaml; return yaml path."""
    from agentshore.sidecar.yaml_edits import write_timelapse as _write

    yaml_path = path / "agentshore.yaml"
    try:
        existing = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
        new_text = _write(existing, timelapse)
        _atomic_write_text(yaml_path, new_text)
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError(f"agentshore.yaml update failed: {exc}", code=-32003) from exc
    return str(yaml_path)


def set_timelapse(payload: object) -> dict[str, object]:
    """Persist the ``timelapse`` block in agentshore.yaml.

    Payload mirrors :class:`TimelapseConfig`::

        {"enabled": bool?, "installed": bool?}

    Returns the persisted values plus the resolved ``yaml_path``.
    """
    path = _require_active()
    timelapse = _validate_timelapse_payload(payload)
    yaml_path = _persist_timelapse(path, timelapse)
    return {
        "timelapse": {"enabled": timelapse.enabled, "installed": timelapse.installed},
        "yaml_path": str(yaml_path),
    }


async def install_timelapse() -> dict[str, object]:
    """Auto-install the timelapse-capture CLI + deps for the active project.

    On success, persists ``timelapse.installed = true`` in agentshore.yaml so
    the desktop can gate the Start-screen toggle. Returns
    ``{"success": bool, "message": str, "installed": bool, "yaml_path": str?}``.
    """
    path = _require_active()
    # Lazy import keeps the sidecar's cold-start import graph light: the
    # installer module is only needed when the user opts into the feature.
    from agentshore.timelapse.setup import install_timelapse as _install

    result = await _install(cwd=path)
    response: dict[str, object] = {
        "success": result.success,
        "message": result.message,
        "installed": result.success,
    }
    if result.success:
        yaml_path = _persist_timelapse(path, TimelapseConfig(enabled=True, installed=True))
        response["yaml_path"] = yaml_path
    return response
