"""project.* RPC method implementations (DESIGN §5.1).

The sidecar maintains a single ``ActiveProject`` slot (§1.3). ``project.select``
sets it, ``project.deselect`` clears it, and ``inspect``/``branches``/
``set_target_branch`` operate against it. Switching projects while a session
is running is the caller's responsibility to gate (§1.3: ``ERR_SESSION_ACTIVE``).
"""

from __future__ import annotations

import contextlib
import io
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from agentshore.budget import MIN_ENABLED_BUDGET_USD
from agentshore.config.models import BudgetConfig

# Internal project.* error codes (mapped to public codes by the dispatcher).
# ERR_PROJECT_NOT_ACTIVE is remapped to server.ERR_NO_ACTIVE_PROJECT (-32011)
# for project.{inspect,branches,set_target_branch}; see server._dispatch.
ERR_PROJECT_NOT_ACTIVE = -32004


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
    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectError(f"path does not exist: {path}", code=-32001) from exc
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


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603, S607 — fixed argv, no shell
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _repo_identity(path: Path) -> dict[str, object]:
    head = _run_git(["rev-parse", "HEAD"], path)
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
        "git": shutil.which("git") is not None,
        "bd": shutil.which("bd") is not None,
        "gh": shutil.which("gh") is not None,
    }


def inspect() -> dict[str, object]:
    """Return the inspection envelope for the active project (DESIGN §5.1)."""
    path = _require_active()
    return {
        "path": str(path),
        "repo_identity": _repo_identity(path),
        "branch": _current_branch(path),
        "detected_tools": _detected_tools(path),
        "agentshore_yaml": _agentshore_yaml_payload(path),
        "beads_status": _beads_status(path),
        "prerequisites": _prerequisites(),
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
    ahead: int
    behind: int


def _ahead_behind(path: Path, ref: str, target: str) -> tuple[int, int]:
    out = _run_git(["rev-list", "--left-right", "--count", f"{ref}...{target}"], path)
    if out.returncode != 0:
        return 0, 0
    parts = out.stdout.strip().split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def branches(*, refresh: bool = False) -> list[_BranchRow]:
    """Return local + remote-tracking branches with ahead/behind vs default.

    When *refresh* is true, run ``git fetch --prune origin`` first so the
    remote-tracking refs reflect upstream state.
    """
    path = _require_active()
    if refresh:
        _run_git(["fetch", "--prune", "origin"], path)

    default = _default_branch_name(path)
    target = f"origin/{default}"
    current_branch = _current_branch(path)

    # Two separate for-each-ref calls so we can tag each ref by source
    # unambiguously. The previous combined call inferred source from the
    # short name's ``origin/`` prefix — which mis-classified a local branch
    # literally named ``origin`` (refname:short == "origin", no slash) as a
    # legitimate local branch row. The user-facing target-branch picker
    # never wants to surface ``origin`` as a selectable branch because it
    # collides with the remote name and would produce ambiguous git
    # operations downstream.
    local_res = _run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        path,
    )
    if local_res.returncode != 0:
        return []
    remote_res = _run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"],
        path,
    )
    if remote_res.returncode != 0:
        return []

    local_names: set[str] = set()
    remote_names: list[str] = []
    local_order: list[str] = []
    for line in local_res.stdout.splitlines():
        ref = line.strip()
        if not ref:
            continue
        # Skip a local branch literally named ``origin`` — it shadows the
        # remote name and is almost always an accidental ref.
        if ref == "origin":
            continue
        if ref not in local_names:
            local_names.add(ref)
            local_order.append(ref)
    for line in remote_res.stdout.splitlines():
        ref = line.strip()
        if not ref:
            continue
        if not ref.startswith("origin/"):
            continue
        name = ref[len("origin/") :]
        if name == "HEAD" or not name:
            continue
        remote_names.append(name)

    rows: list[_BranchRow] = []
    for name in local_order:
        ahead, behind = _ahead_behind(path, name, target)
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
    for name in remote_names:
        if name in local_names:
            continue
        ahead, behind = _ahead_behind(path, f"origin/{name}", target)
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
    return rows


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


def _write_target_branch(yaml_text: str, branch: str) -> str:
    """Round-trip *yaml_text* and set ``project.target_branch`` to *branch*.

    Preserves comments and key ordering via ruamel.yaml. If the document is
    empty or has no ``project`` key, a minimal one is created.
    """
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    data = rt.load(yaml_text) if yaml_text.strip() else None
    if data is None:
        data = {}
    project = data.get("project")
    if not isinstance(project, dict):
        project = {}
        data["project"] = project
    project["target_branch"] = branch
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


def set_target_branch(name: str) -> dict[str, object]:
    """Persist *name* as ``project.target_branch`` in agentshore.yaml (DESIGN §4.1).

    Validates that ``origin`` has the branch before writing. Write is atomic
    (temp + fsync + rename).
    """
    path = _require_active()
    if not isinstance(name, str) or not name.strip():
        raise ProjectError("name must be a non-empty string")
    name = name.strip()
    res = _run_git(["ls-remote", "--exit-code", "--heads", "origin", name], path)
    if res.returncode != 0:
        raise ProjectError(
            f"branch '{name}' not found on origin",
            code=-32002,
        )
    yaml_path = path / "agentshore.yaml"
    try:
        existing = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
        new_text = _write_target_branch(existing, name)
        _atomic_write_text(yaml_path, new_text)
    except Exception as exc:
        raise ProjectError(f"agentshore.yaml update failed: {exc}", code=-32003) from exc
    return {"target_branch": name, "yaml_path": str(yaml_path)}


def _write_seed_paths(yaml_text: str, seed_paths: list[str]) -> str:
    """Round-trip *yaml_text* and set ``intake.seed_paths`` to *seed_paths*.

    Preserves comments and key ordering via ruamel.yaml. Mirrors
    :func:`_write_target_branch`; creates the ``intake`` mapping if absent.
    """
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    data = rt.load(yaml_text) if yaml_text.strip() else None
    if data is None:
        data = {}
    intake = data.get("intake")
    if not isinstance(intake, dict):
        intake = {}
        data["intake"] = intake
    intake["seed_paths"] = list(seed_paths)
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


def set_seed_paths(payload: object) -> dict[str, object]:
    """Persist seed material paths as ``intake.seed_paths`` in agentshore.yaml.

    Accepts a single path string or a list of strings (relative to the project
    root, or absolute). Each must be a non-empty string and exist on disk. An
    empty list clears the configured seed. Write is atomic (temp + fsync +
    rename). Mirrors :func:`set_target_branch` so any start path — CLI, sidecar,
    desktop Quick Start, TUI — picks up the configured seed via the bootstrap
    ``_resolve_seed_path`` fallback.
    """
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
        new_text = _write_seed_paths(existing, seed_paths)
        _atomic_write_text(yaml_path, new_text)
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError(f"agentshore.yaml update failed: {exc}", code=-32003) from exc
    return {"seed_paths": seed_paths, "yaml_path": str(yaml_path)}


# Accepted keys on the ``budget:`` mapping written to agentshore.yaml. Mirrors
# the ``BudgetConfig`` dataclass in ``src/agentshore/config/models.py``.
_BUDGET_KEYS: frozenset[str] = frozenset({"enabled", "total", "warning_threshold"})


def _write_budget(yaml_text: str, budget: BudgetConfig) -> str:
    """Round-trip *yaml_text* and set the top-level ``budget`` mapping.

    Preserves comments and key ordering on every other section via
    ruamel.yaml. Mirrors :func:`_write_target_branch` so the two writers
    share a code shape and a serialiser.
    """
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    data = rt.load(yaml_text) if yaml_text.strip() else None
    if data is None:
        data = {}
    existing = data.get("budget")
    budget_block: dict[str, object] = existing if isinstance(existing, dict) else {}
    budget_block["enabled"] = bool(budget.enabled)
    budget_block["total"] = float(budget.total)
    budget_block["warning_threshold"] = float(budget.warning_threshold)
    data["budget"] = budget_block
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


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
    return BudgetConfig(enabled=enabled, total=total, warning_threshold=threshold)


def set_budget(payload: object) -> dict[str, object]:
    """Persist the ``budget`` block in agentshore.yaml (issue #571 follow-up).

    Validates the incoming payload and atomically rewrites the ``budget:``
    key. Other keys / comments / ordering are preserved via ruamel.yaml.

    The payload shape mirrors :class:`BudgetConfig` in
    ``src/agentshore/config/models.py``::

        {"enabled": bool, "total": float, "warning_threshold": float?}

    Returns the persisted values (echoing what was written) plus the
    resolved ``yaml_path``.
    """
    path = _require_active()
    budget = _validate_budget_payload(payload)
    yaml_path = path / "agentshore.yaml"
    try:
        existing = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
        new_text = _write_budget(existing, budget)
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
        },
        "yaml_path": str(yaml_path),
    }
