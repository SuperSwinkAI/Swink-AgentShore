"""project.* RPC method implementations (DESIGN Â§5.1).

The sidecar maintains a single ``ActiveProject`` slot (Â§1.3). ``project.select``
sets it, ``project.deselect`` clears it, and ``inspect``/``branches``/
``set_target_branch`` operate against it. Switching projects while a session
is running is the caller's responsibility to gate (Â§1.3: ``ERR_SESSION_ACTIVE``).
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from agentshore import subprocess_env
from agentshore.beads import resolve_bd_binary
from agentshore.budget import validate_budget_payload
from agentshore.command import CommandResult, git_sync
from agentshore.config.models import BudgetConfig, TimelapseConfig

# Internal project.* error codes (mapped to public codes by the dispatcher).
# ERR_PROJECT_NOT_ACTIVE is remapped to server.ERR_NO_ACTIVE_PROJECT (-32011)
# for project.{inspect,branches,set_target_branch}; see server._dispatch.
ERR_PROJECT_NOT_ACTIVE = -32004
GIT_PROBE_TIMEOUT_SECONDS = 5.0
GIT_TIMEOUT_RETURN_CODE = 124
# Deliberately short: inspect() fans probes out concurrently and returns within
# this deadline with filesystem fallbacks for any slow probe, so the Readiness
# screen always paints fast and degrades to a "probe timed out, re-run" state
# rather than blocking. The git layer's non-interactive env (no credential
# prompt) is what removes the real hang risk — not a longer deadline here.
INSPECT_PROBE_TIMEOUT_SECONDS = 2.0
BRANCH_LIST_TIMEOUT_SECONDS = 20.0
BRANCH_REMOTE_ONLY_LIMIT = 200
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


def _collect_probe[T](
    future: concurrent.futures.Future[T],
    fallback: T,
    deadline: float,
) -> T:
    remaining = max(0.0, deadline - time.monotonic())
    try:
        return future.result(timeout=remaining)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return fallback
    except Exception:
        return fallback


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


def inspect() -> dict[str, object]:
    """Return the inspection envelope for the active project (DESIGN Â§5.1)."""
    path = _require_active()
    deadline = time.monotonic() + INSPECT_PROBE_TIMEOUT_SECONDS
    repo_identity = _PROBE_EXECUTOR.submit(lambda: _repo_identity(path))
    current_branch = _PROBE_EXECUTOR.submit(lambda: _current_branch(path))
    detected_tools = _PROBE_EXECUTOR.submit(lambda: _detected_tools(path))
    agentshore_yaml = _PROBE_EXECUTOR.submit(lambda: _agentshore_yaml_payload(path))
    beads_status = _PROBE_EXECUTOR.submit(lambda: _beads_status(path))
    prerequisites = _PROBE_EXECUTOR.submit(_prerequisites)

    return {
        "path": str(path),
        "repo_identity": _collect_probe(
            repo_identity,
            _repo_identity_probe_fallback(path),
            deadline,
        ),
        "branch": _collect_probe(current_branch, None, deadline),
        "detected_tools": _collect_probe(detected_tools, [], deadline),
        "agentshore_yaml": _collect_probe(
            agentshore_yaml,
            None,
            deadline,
        ),
        "beads_status": _collect_probe(
            beads_status,
            {"initialised": False, "probe_error": "beads status probe timed out"},
            deadline,
        ),
        "prerequisites": _collect_probe(
            prerequisites,
            {
                "git": False,
                "bd": False,
                "gh": False,
                "probe_error": "tooling probe timed out",
            },
            deadline,
        ),
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


def _ahead_behind(path: Path, ref: str, target: str, *, deadline: float) -> tuple[int, int]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0, 0
    out = _run_git(
        ["rev-list", "--left-right", "--count", f"{ref}...{target}"],
        path,
        timeout_seconds=min(GIT_PROBE_TIMEOUT_SECONDS, max(0.1, remaining)),
    )
    if out.returncode != 0:
        return 0, 0
    parts = out.stdout.strip().split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def _read_small_text(path: Path, *, limit: int = 64 * 1024) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return None


def _git_dir_for_worktree(path: Path) -> Path | None:
    dot_git = path / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    text = _read_small_text(dot_git, limit=4096)
    if text is None:
        return None
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    if not first.lower().startswith("gitdir:"):
        return None
    raw = first[len("gitdir:") :].strip()
    if not raw:
        return None
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        git_dir = dot_git.parent / git_dir
    return git_dir


def _common_git_dir(git_dir: Path) -> Path:
    text = _read_small_text(git_dir / "commondir", limit=4096)
    if text is None:
        return git_dir
    raw = text.splitlines()[0].strip() if text.splitlines() else ""
    if not raw:
        return git_dir
    common = Path(raw)
    if not common.is_absolute():
        common = git_dir / common
    return common


def _read_ref_names(common_git_dir: Path, prefix: str) -> list[str]:
    names: set[str] = set()
    root = common_git_dir.joinpath(*prefix.split("/"))
    if root.is_dir():
        for ref_file in root.rglob("*"):
            if not ref_file.is_file() or ref_file.name.endswith(".lock"):
                continue
            with contextlib.suppress(ValueError):
                name = ref_file.relative_to(root).as_posix()
                if name and name != "HEAD":
                    names.add(name)

    packed = _read_small_text(common_git_dir / "packed-refs")
    if packed is not None:
        packed_prefix = f"{prefix}/"
        for raw_line in packed.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "^")):
                continue
            parts = line.split()
            if len(parts) != 2:
                continue
            ref = parts[1]
            if ref.startswith(packed_prefix):
                name = ref[len(packed_prefix) :]
                if name and name != "HEAD":
                    names.add(name)
    return sorted(names)


def _symbolic_ref_name(git_dir: Path, ref_path: str, prefix: str) -> str | None:
    text = _read_small_text(git_dir.joinpath(*ref_path.split("/")), limit=4096)
    if text is None:
        return None
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    if not first.startswith("ref:"):
        return None
    ref = first[len("ref:") :].strip()
    full_prefix = f"{prefix}/"
    if ref.startswith(full_prefix):
        return ref[len(full_prefix) :]
    return None


def _filesystem_branches(path: Path) -> tuple[str, str | None, list[str], list[str]]:
    git_dir = _git_dir_for_worktree(path)
    if git_dir is None:
        return "main", None, [], []
    common_git_dir = _common_git_dir(git_dir)
    local_names = [
        name for name in _read_ref_names(common_git_dir, "refs/heads") if name != "origin"
    ]
    remote_names = _read_ref_names(common_git_dir, "refs/remotes/origin")
    current = _symbolic_ref_name(git_dir, "HEAD", "refs/heads")
    default = _symbolic_ref_name(common_git_dir, "refs/remotes/origin/HEAD", "refs/remotes/origin")
    if default is None:
        if "main" in local_names or "main" in remote_names:
            default = "main"
        elif "master" in local_names or "master" in remote_names:
            default = "master"
        elif current is not None:
            default = current
        elif local_names:
            default = local_names[0]
        elif remote_names:
            default = remote_names[0]
        else:
            default = "main"
    return default, current, local_names, remote_names


def _remaining_timeout(deadline: float) -> float:
    return max(0.1, min(GIT_PROBE_TIMEOUT_SECONDS, deadline - time.monotonic()))


def _branch_rows(
    path: Path,
    *,
    default: str,
    current_branch: str | None,
    local_order: list[str],
    remote_names: list[str],
    deadline: float | None,
) -> list[_BranchRow]:
    target = f"origin/{default}"
    rows: list[_BranchRow] = []
    local_names = set(local_order)
    for name in local_order:
        ahead, behind = (0, 0)
        if deadline is not None:
            ahead, behind = _ahead_behind(path, name, target, deadline=deadline)
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
        ahead, behind = (0, 0)
        if deadline is not None:
            ahead, behind = _ahead_behind(path, f"origin/{name}", target, deadline=deadline)
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


def _filesystem_branch_rows(path: Path) -> list[_BranchRow]:
    default, current_branch, local_order, remote_names = _filesystem_branches(path)
    return _branch_rows(
        path,
        default=default,
        current_branch=current_branch,
        local_order=local_order,
        remote_names=remote_names,
        deadline=None,
    )


def branches(*, refresh: bool = False) -> list[_BranchRow]:
    """Return local + remote-tracking branches for the target-branch picker.

    The default setup-screen load is filesystem-only so Windows cannot hang in
    Git process startup or network-adjacent probes. ``refresh=True`` preserves
    the Mac-era richer path: fetch origin, ask Git for refs, and compute
    ahead/behind where possible, all under one shared deadline.
    """
    path = _require_active()
    if not refresh:
        return _filesystem_branch_rows(path)

    deadline = time.monotonic() + BRANCH_LIST_TIMEOUT_SECONDS
    fetch_res = _run_git(
        ["fetch", "--prune", "origin"],
        path,
        timeout_seconds=max(0.1, deadline - time.monotonic()),
    )
    if fetch_res.returncode == GIT_TIMEOUT_RETURN_CODE or time.monotonic() >= deadline:
        return _filesystem_branch_rows(path)

    default, current_branch, fallback_local, fallback_remote = _filesystem_branches(path)
    local_res = _run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        path,
        timeout_seconds=_remaining_timeout(deadline),
    )
    if time.monotonic() >= deadline:
        return _filesystem_branch_rows(path)
    remote_res = _run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"],
        path,
        timeout_seconds=_remaining_timeout(deadline),
    )
    if local_res.returncode != 0 or remote_res.returncode != 0:
        local_order = fallback_local
        remote_names = fallback_remote
    else:
        local_seen: set[str] = set()
        local_order = []
        remote_names = []
        for line in local_res.stdout.splitlines():
            ref = line.strip()
            if not ref or ref == "origin":
                continue
            if ref not in local_seen:
                local_seen.add(ref)
                local_order.append(ref)
        for line in remote_res.stdout.splitlines():
            ref = line.strip()
            if not ref or not ref.startswith("origin/"):
                continue
            name = ref[len("origin/") :]
            if name and name != "HEAD":
                remote_names.append(name)

    return _branch_rows(
        path,
        default=default,
        current_branch=current_branch,
        local_order=local_order,
        remote_names=remote_names,
        deadline=deadline,
    )


def _is_valid_branch_name(name: str) -> bool:
    """Conservative branch-name validation without invoking git.

    The desktop setup flow must not block on git/ref/remote probes on Windows.
    This accepts normal branch paths such as ``main`` and ``feature/x`` while
    rejecting the refname shapes Git refuses or that are unsafe to persist.
    """
    if not name or name in {".", "..", "@", "HEAD"}:
        return False
    if name.startswith(("/", "-", ".")) or name.endswith(("/", ".", ".lock")):
        return False
    if any(ord(ch) < 32 or ch in {" ", "~", "^", ":", "?", "*", "[", "\\", "\x7f"} for ch in name):
        return False
    if any(part in {"", ".", ".."} or part.endswith(".lock") for part in name.split("/")):
        return False
    return not (".." in name or "@{" in name or "//" in name)


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
    """Persist *name* as ``project.target_branch`` in agentshore.yaml (DESIGN Â§4.1).

    Validates against existing local or ``origin/*`` refs using the same
    filesystem-backed branch reader as ``project.branches``. This preserves the
    setup contract ("choose an existing branch") without invoking Git
    subprocesses or network checks on Windows.
    """
    path = _require_active()
    if not isinstance(name, str) or not name.strip():
        raise ProjectError("name must be a non-empty string")
    name = name.strip()
    if not _is_valid_branch_name(name):
        raise ProjectError(
            f"invalid branch name: {name!r}",
            code=-32002,
        )
    _default, _current, local_names, remote_names = _filesystem_branches(path)
    if name not in set(local_names) | set(remote_names):
        raise ProjectError(
            f"branch '{name}' not found in local or origin-tracking refs",
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
    rename). Mirrors :func:`set_target_branch` so any start path â€” CLI, sidecar,
    desktop Quick Start, TUI â€” picks up the configured seed via the bootstrap
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


def _write_budget(yaml_text: str, budget: BudgetConfig) -> str:
    """Round-trip *yaml_text* and set the top-level ``budget`` mapping.

    Delegates to :func:`agentshore.config.budget_writer.render_budget_yaml` so
    the sidecar RPC and the live ``Orchestrator.set_budget`` path share one
    serialiser. Preserves comments / key ordering on every other section.
    """
    from agentshore.config.budget_writer import render_budget_yaml

    return render_budget_yaml(yaml_text, budget)


def _validate_budget_payload(payload: object) -> BudgetConfig:
    """Validate the ``set_budget`` payload and return a :class:`BudgetConfig`.

    Delegates to :func:`agentshore.budget.validate_budget_payload`, raising
    :class:`ProjectError` for any invalid value.
    """
    return validate_budget_payload(payload, exc_class=ProjectError)


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
            "time_enabled": budget.time_enabled,
            "time_total_minutes": budget.time_total_minutes,
        },
        "yaml_path": str(yaml_path),
    }


def _write_trusted_issue_enforcement(yaml_text: str, enabled: bool) -> str:
    """Round-trip *yaml_text* and set ``trusted_ids.restrict_issues_to_trusted_authors``.

    Preserves comments and key ordering on every other section via
    ruamel.yaml. Mirrors :func:`_write_budget`/:func:`_write_target_branch`;
    get-or-creates the ``trusted_ids`` mapping if absent.
    """
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    data = rt.load(yaml_text) if yaml_text.strip() else None
    if data is None:
        data = {}
    trusted_ids = data.get("trusted_ids")
    if not isinstance(trusted_ids, dict):
        trusted_ids = {}
        data["trusted_ids"] = trusted_ids
    trusted_ids["restrict_issues_to_trusted_authors"] = bool(enabled)
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


def set_trusted_issue_enforcement(payload: object) -> dict[str, object]:
    """Persist ``trusted_ids.restrict_issues_to_trusted_authors`` in agentshore.yaml.

    The desktop toggles "only work issues from trusted identities" via this
    method. The dispatcher extracts ``enabled`` and passes the bare bool, so
    *payload* must be a bool. Write is atomic (temp + fsync + rename); other
    keys / comments / ordering are preserved via ruamel.yaml.

    Returns ``{"enabled": bool, "yaml_path": str}``.
    """
    path = _require_active()
    if not isinstance(payload, bool):
        raise ProjectError("enabled must be a boolean")
    enabled = payload
    yaml_path = path / "agentshore.yaml"
    try:
        existing = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
        new_text = _write_trusted_issue_enforcement(existing, enabled)
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


def _write_timelapse(yaml_text: str, timelapse: TimelapseConfig) -> str:
    """Round-trip *yaml_text* and set the top-level ``timelapse`` mapping.

    Preserves comments / key ordering on every other section via ruamel.yaml,
    matching :func:`_write_budget`.
    """
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    data = rt.load(yaml_text) if yaml_text.strip() else None
    if data is None:
        data = {}
    existing = data.get("timelapse")
    block: dict[str, object] = existing if isinstance(existing, dict) else {}
    block["enabled"] = bool(timelapse.enabled)
    block["installed"] = bool(timelapse.installed)
    data["timelapse"] = block
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


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
    yaml_path = path / "agentshore.yaml"
    try:
        existing = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
        new_text = _write_timelapse(existing, timelapse)
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
