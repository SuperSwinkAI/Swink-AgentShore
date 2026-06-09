"""Tests for the sidecar ``project.*`` RPC family (DESIGN §5.1)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import cast

import pytest

from agentshore.sidecar import project as project_rpc
from agentshore.sidecar.handshake import capabilities
from agentshore.sidecar.server import (
    ERR_NO_ACTIVE_PROJECT,
    ERR_SESSION_ACTIVE,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    ServerState,
    handle_request,
)


class _StubStore:
    """Stand-in for ``DataStore`` that records ``close()`` calls."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _drive(payload: dict[str, object], *, state: ServerState | None = None) -> dict[str, object]:
    """Run ``handle_request`` and await any coroutine response."""
    response = handle_request(payload, state=state)
    if asyncio.iscoroutine(response):
        response = asyncio.run(response)
    assert response is not None
    return response


@pytest.fixture(autouse=True)
def _reset_active() -> None:
    project_rpc.reset_state_for_tests()
    yield
    project_rpc.reset_state_for_tests()


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A minimal git repo with origin + an extra branch."""
    upstream = tmp_path / "upstream.git"
    _git(["init", "--bare", "-b", "main", str(upstream)], tmp_path)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main", str(repo)], tmp_path)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "init"], repo)
    _git(["remote", "add", "origin", str(upstream)], repo)
    _git(["push", "-u", "origin", "main"], repo)

    # Second branch on origin
    _git(["checkout", "-b", "feature/x"], repo)
    (repo / "f.txt").write_text("f\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-m", "f"], repo)
    _git(["push", "-u", "origin", "feature/x"], repo)
    _git(["checkout", "main"], repo)
    _git(["fetch", "origin"], repo)

    # Point origin/HEAD so default-branch detection works.
    _git(["remote", "set-head", "origin", "main"], repo)
    return repo


# ---------------------------------------------------------------------------
# project.select / deselect / current
# ---------------------------------------------------------------------------


def test_capabilities_advertises_project_methods() -> None:
    caps = capabilities()
    for method in (
        "project.select",
        "project.inspect",
        "project.branches",
        "project.set_target_branch",
        "project.set_trusted_issue_enforcement",
        "project.deselect",
    ):
        assert method in caps


def test_select_sets_active_project(tmp_path: Path) -> None:
    result = project_rpc.select(str(tmp_path))
    assert result["path"] == str(tmp_path.resolve())
    active = project_rpc.current()
    assert active is not None
    assert active.path == tmp_path.resolve()


def test_select_is_idempotent(tmp_path: Path) -> None:
    first = project_rpc.select(str(tmp_path))
    second = project_rpc.select(str(tmp_path))
    assert first == second
    assert project_rpc.current() is not None


def test_select_rejects_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.select(str(missing))


def test_select_rejects_file_path(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.select(str(f))


def test_select_rejects_empty_path() -> None:
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.select("")


def test_deselect_clears_active(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    assert project_rpc.current() is not None
    project_rpc.deselect()
    assert project_rpc.current() is None


def test_deselect_is_noop_when_unset() -> None:
    project_rpc.deselect()
    assert project_rpc.current() is None


# ---------------------------------------------------------------------------
# project.inspect
# ---------------------------------------------------------------------------


def test_inspect_without_active_raises() -> None:
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.inspect()


def test_inspect_returns_envelope_for_git_repo(git_repo: Path) -> None:
    (git_repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    (git_repo / "agentshore.yaml").write_text("project:\n  path: .\n")
    (git_repo / ".beads").mkdir()

    project_rpc.select(str(git_repo))
    payload = project_rpc.inspect()

    assert payload["path"] == str(git_repo.resolve())
    assert payload["branch"] == "main"

    identity = cast("dict[str, object]", payload["repo_identity"])
    assert identity["is_git"] is True
    assert isinstance(identity["head_sha"], str) and len(cast("str", identity["head_sha"])) == 40

    assert "python" in cast("list[str]", payload["detected_tools"])

    yaml_payload = cast("dict[str, object]", payload["agentshore_yaml"])
    assert yaml_payload["path"].endswith("agentshore.yaml")  # type: ignore[union-attr]
    assert "project:" in cast("str", yaml_payload["raw"])

    beads = cast("dict[str, object]", payload["beads_status"])
    assert beads["initialised"] is True

    prereqs = cast("dict[str, object]", payload["prerequisites"])
    assert "git" in prereqs


def test_prerequisites_uses_managed_bd_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolve(name: str) -> str | None:
        if name in {"git", "gh"}:
            return f"/usr/bin/{name}"
        return None

    monkeypatch.setattr(project_rpc.subprocess_env, "resolve_tool", fake_resolve)
    monkeypatch.setattr(
        project_rpc, "resolve_bd_binary", lambda: r"C:\Users\x\AppData\Local\Programs\bd\bd.exe"
    )

    assert project_rpc._prerequisites() == {"git": True, "bd": True, "gh": True}  # noqa: SLF001


def test_inspect_handles_non_git_path(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    payload = project_rpc.inspect()
    identity = cast("dict[str, object]", payload["repo_identity"])
    assert identity["is_git"] is False
    assert payload["agentshore_yaml"] is None
    assert cast("dict[str, object]", payload["beads_status"])["initialised"] is False


def test_run_git_times_out_instead_of_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resolve git to a fixed fake path so the probe is hermetic regardless of
    # whether git is installed on the test box, then make the spawn time out.
    monkeypatch.setattr(
        "agentshore.subprocess_env.resolve_tool",
        lambda name: "/usr/bin/git" if name == "git" else None,
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["git", "rev-parse", "HEAD"], timeout=0.01)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = project_rpc._run_git(  # noqa: SLF001 - regression coverage for probe boundary
        ["rev-parse", "HEAD"],
        tmp_path,
        timeout_seconds=0.01,
    )

    assert result.returncode == project_rpc.GIT_TIMEOUT_RETURN_CODE
    assert "timed out" in result.stderr


def test_inspect_treats_git_probe_timeout_as_unknown_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_git(
        args: list[str],
        cwd: Path,
        *,
        timeout_seconds: float = project_rpc.GIT_PROBE_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout_seconds
        if args == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                ["git", *args],
                project_rpc.GIT_TIMEOUT_RETURN_CODE,
                stdout="",
                stderr="git rev-parse HEAD timed out",
            )
        return subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")

    monkeypatch.setattr(project_rpc, "_run_git", fake_run_git)

    project_rpc.select(str(tmp_path))
    payload = project_rpc.inspect()

    identity = cast("dict[str, object]", payload["repo_identity"])
    assert identity["is_git"] is True
    assert identity["root"] == str(tmp_path.resolve())
    assert identity["head_sha"] == ""
    assert "timed out" in cast("str", identity["probe_error"])


def test_inspect_times_out_slow_readiness_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_repo_identity(_path: Path) -> dict[str, object]:
        time.sleep(5)
        return {"is_git": False}

    monkeypatch.setattr(project_rpc, "_repo_identity", slow_repo_identity)
    project_rpc.select(str(tmp_path))

    started = time.monotonic()
    payload = project_rpc.inspect()
    elapsed = time.monotonic() - started

    assert elapsed < 3
    identity = cast("dict[str, object]", payload["repo_identity"])
    assert identity["is_git"] is True
    assert "timed out" in cast("str", identity["probe_error"])


# ---------------------------------------------------------------------------
# project.branches
# ---------------------------------------------------------------------------


def test_branches_lists_local_and_remote(git_repo: Path) -> None:
    project_rpc.select(str(git_repo))
    rows = project_rpc.branches()

    names = {row["name"] for row in rows}
    assert "main" in names
    assert "feature/x" in names

    main_row = next(r for r in rows if r["name"] == "main")
    assert main_row["is_default"] is True
    assert main_row["is_current"] is True
    assert main_row["is_remote"] is False

    feature_row = next(r for r in rows if r["name"] == "feature/x")
    assert feature_row["is_default"] is False
    assert feature_row["is_current"] is False


def test_branches_passive_load_does_not_probe_git(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_run_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("passive target-branch load should not spawn git")

    monkeypatch.setattr(project_rpc, "_run_git", fail_run_git)
    project_rpc.select(str(git_repo))

    rows = project_rpc.branches()

    assert {row["name"] for row in rows} >= {"main", "feature/x"}
    assert all(row["ahead"] == 0 and row["behind"] == 0 for row in rows)


def test_branches_refresh_reports_ahead_behind(git_repo: Path) -> None:
    # Add an extra commit on main locally. Explicit refresh preserves the richer
    # Mac-era Git metadata path while the passive setup load stays filesystem-only.
    (git_repo / "extra.txt").write_text("e\n")
    _git(["add", "extra.txt"], git_repo)
    _git(["commit", "-m", "extra"], git_repo)

    project_rpc.select(str(git_repo))
    rows = project_rpc.branches(refresh=True)

    main_row = next(r for r in rows if r["name"] == "main" and not r["is_remote"])
    assert main_row["ahead"] == 1
    assert main_row["behind"] == 0


def test_ahead_behind_skips_when_deadline_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_run_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("expired branch deadline should not run git")

    monkeypatch.setattr(project_rpc, "_run_git", fail_run_git)

    assert project_rpc._ahead_behind(  # noqa: SLF001 - regression coverage for timeout boundary
        tmp_path,
        "main",
        "origin/main",
        deadline=time.monotonic() - 1,
    ) == (0, 0)


def test_branches_refresh_fetches_and_bounds_git_probes(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_rpc.select(str(git_repo))
    calls: list[tuple[list[str], float]] = []

    def fake_run_git(
        args: list[str],
        _cwd: Path,
        *,
        timeout_seconds: float = project_rpc.GIT_PROBE_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, timeout_seconds))
        stdout = ""
        if args == ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]:
            stdout = "origin/main\n"
        elif args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            stdout = "main\n"
        elif args == ["for-each-ref", "--format=%(refname:short)", "refs/heads/"]:
            stdout = "main\nfeature/x\n"
        elif args == ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"]:
            stdout = "origin/HEAD\norigin/main\norigin/feature/x\n"
        elif args[0] == "rev-list":
            stdout = "0 0\n"
        return subprocess.CompletedProcess(["git", *args], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(project_rpc, "_run_git", fake_run_git)
    rows = project_rpc.branches(refresh=True)

    assert rows[0]["name"] == "main"
    assert calls[0][0] == ["fetch", "--prune", "origin"]
    assert 0 < calls[0][1] <= project_rpc.BRANCH_LIST_TIMEOUT_SECONDS
    called_args = [call[0] for call in calls]
    assert ["for-each-ref", "--format=%(refname:short)", "refs/heads/"] in called_args
    assert ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"] in called_args


# ---------------------------------------------------------------------------
# project.set_target_branch
# ---------------------------------------------------------------------------


def test_set_target_branch_writes_yaml(git_repo: Path) -> None:
    yaml_path = git_repo / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n  goals: ship it\n")
    project_rpc.select(str(git_repo))

    result = project_rpc.set_target_branch("feature/x")
    assert result["target_branch"] == "feature/x"

    new_text = yaml_path.read_text()
    assert "target_branch: feature/x" in new_text
    # Pre-existing keys preserved (ruamel round-trip).
    assert "path: ." in new_text
    assert "goals: ship it" in new_text


def test_set_target_branch_does_not_probe_git_refs(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = git_repo / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n")

    def spy_run_git(
        _args: list[str],
        _cwd: Path,
        *,
        timeout_seconds: float = project_rpc.GIT_PROBE_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("set_target_branch should not probe git refs during setup")

    monkeypatch.setattr(project_rpc, "_run_git", spy_run_git)

    project_rpc.select(str(git_repo))
    result = project_rpc.set_target_branch("main")

    assert result["target_branch"] == "main"


def test_set_target_branch_creates_yaml_when_missing(git_repo: Path) -> None:
    yaml_path = git_repo / "agentshore.yaml"
    assert not yaml_path.exists()
    project_rpc.select(str(git_repo))
    project_rpc.set_target_branch("main")
    assert yaml_path.exists()
    assert "target_branch: main" in yaml_path.read_text()


def test_set_target_branch_rejects_unfetched_branch(git_repo: Path) -> None:
    (git_repo / "agentshore.yaml").write_text("project:\n")
    project_rpc.select(str(git_repo))
    with pytest.raises(project_rpc.ProjectError) as info:
        project_rpc.set_target_branch("does-not-exist-yet")
    assert info.value.code == -32002


@pytest.mark.parametrize(
    "branch_name",
    ["has space", "-bad", "bad..name", "bad@{name", "bad.lock", "bad\\name", "bad:name"],
)
def test_set_target_branch_rejects_invalid_branch_name(
    git_repo: Path,
    branch_name: str,
) -> None:
    project_rpc.select(str(git_repo))
    with pytest.raises(project_rpc.ProjectError) as info:
        project_rpc.set_target_branch(branch_name)
    assert info.value.code == -32002


def test_set_target_branch_rejects_empty_name(git_repo: Path) -> None:
    project_rpc.select(str(git_repo))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_target_branch("   ")


def test_set_target_branch_maps_invalid_yaml_to_agentshore_yaml_error(git_repo: Path) -> None:
    yaml_path = git_repo / "agentshore.yaml"
    yaml_path.write_text("project: [\n", encoding="utf-8")
    project_rpc.select(str(git_repo))

    with pytest.raises(project_rpc.ProjectError) as info:
        project_rpc.set_target_branch("main")
    assert info.value.code == -32003
    assert "agentshore.yaml update failed:" in str(info.value)


def test_set_target_branch_maps_directory_yaml_path_to_agentshore_yaml_error(
    git_repo: Path,
) -> None:
    yaml_path = git_repo / "agentshore.yaml"
    yaml_path.mkdir()
    project_rpc.select(str(git_repo))

    with pytest.raises(project_rpc.ProjectError) as info:
        project_rpc.set_target_branch("main")
    assert info.value.code == -32003
    assert "agentshore.yaml update failed:" in str(info.value)


def test_set_target_branch_round_trips_through_config_loader(git_repo: Path) -> None:
    """The YAML key the sidecar writes must deserialize into ProjectConfig.

    Regression for desktop-3t62 / desktop-53m0: ``project.set_target_branch``
    has always written the YAML key, but ``ProjectConfig`` previously had no
    field for it, so the value was silently dropped on every config load.
    """
    from agentshore.config import load_config

    yaml_path = git_repo / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n  goals: ship it\n")
    project_rpc.select(str(git_repo))

    project_rpc.set_target_branch("feature/x")

    cfg = load_config(yaml_path)
    assert cfg.project.target_branch == "feature/x"
    # Goals + path round-trip too — ruamel preserved them through the write.
    assert cfg.project.goals == "ship it"
    assert cfg.project.path == "."


def test_project_config_target_branch_defaults_to_none_when_yaml_absent_field(
    tmp_path: Path,
) -> None:
    """Configs without ``project.target_branch`` deserialize as ``None``.

    Existing projects on disk must continue to load and run with default-branch
    fallback semantics (desktop-53m0).
    """
    from agentshore.config import load_config

    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n  goals: null\n")
    cfg = load_config(yaml_path)
    assert cfg.project.target_branch is None


def test_project_config_target_branch_blank_string_normalized_to_none(
    tmp_path: Path,
) -> None:
    """Whitespace-only ``target_branch:`` values normalise to ``None``.

    Callers can safely use ``cfg.project.target_branch or <fallback>`` without
    re-checking truthiness.
    """
    from agentshore.config import load_config

    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text('project:\n  target_branch: "   "\n')
    cfg = load_config(yaml_path)
    assert cfg.project.target_branch is None


# ---------------------------------------------------------------------------
# project.set_budget (issue #571 follow-up)
# ---------------------------------------------------------------------------


def test_set_budget_writes_capped_block(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n  goals: ship it\n")
    project_rpc.select(str(tmp_path))

    result = project_rpc.set_budget({"enabled": True, "total": 250.0})

    budget = cast("dict[str, object]", result["budget"])
    assert budget["enabled"] is True
    assert budget["total"] == 250.0
    assert budget["warning_threshold"] == 0.20
    new_text = yaml_path.read_text()
    assert "budget:" in new_text
    assert "enabled: true" in new_text
    assert "total: 250" in new_text
    # Pre-existing keys preserved by ruamel.yaml round-trip.
    assert "path: ." in new_text
    assert "goals: ship it" in new_text


def test_set_budget_writes_disabled_block(tmp_path: Path) -> None:
    """``enabled=False`` + ``total=0`` mirrors the BudgetConfig default."""
    yaml_path = tmp_path / "agentshore.yaml"
    project_rpc.select(str(tmp_path))

    result = project_rpc.set_budget({"enabled": False, "total": 0})

    budget = cast("dict[str, object]", result["budget"])
    assert budget["enabled"] is False
    assert budget["total"] == 0.0
    new_text = yaml_path.read_text()
    assert "enabled: false" in new_text
    assert "total: 0" in new_text


def test_set_budget_creates_yaml_when_missing(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    assert not yaml_path.exists()
    project_rpc.select(str(tmp_path))

    project_rpc.set_budget({"enabled": True, "total": 100})

    assert yaml_path.exists()
    text = yaml_path.read_text()
    assert "budget:" in text
    assert "total: 100" in text


def test_set_budget_preserves_other_top_level_keys(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "project:\n  path: .\n"
        "agents:\n  claude_code:\n    enabled: true\n"
        "budget:\n  enabled: false\n  total: 0.0\n"
    )
    project_rpc.select(str(tmp_path))

    project_rpc.set_budget({"enabled": True, "total": 400})

    text = yaml_path.read_text()
    assert "project:" in text
    assert "path: ." in text
    assert "agents:" in text
    assert "claude_code:" in text
    assert "enabled: true" in text  # only one row that's true is set on budget
    assert "total: 400" in text


def test_set_budget_accepts_warning_threshold_override(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    result = project_rpc.set_budget({"enabled": True, "total": 50, "warning_threshold": 0.5})
    budget = cast("dict[str, object]", result["budget"])
    assert budget["warning_threshold"] == 0.5


def test_set_budget_rejects_negative_total(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError) as info:
        project_rpc.set_budget({"enabled": True, "total": -1})
    assert "must be >= 0" in str(info.value)


@pytest.mark.parametrize("total", [0, 0.01, 5, 10, 19.99])
def test_set_budget_rejects_enabled_below_min(tmp_path: Path, total: float) -> None:
    """Enabling a budget under MIN_ENABLED_BUDGET_USD must be rejected.

    The sidecar must mirror ``agentshore.config._parse_budget``'s contract so
    payloads it persists round-trip cleanly through ``load_config``.
    """
    from agentshore.budget import MIN_ENABLED_BUDGET_USD

    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError) as info:
        project_rpc.set_budget({"enabled": True, "total": total})
    message = str(info.value)
    assert "at least" in message
    assert f"{MIN_ENABLED_BUDGET_USD:.2f}" in message
    assert "budget.enabled is true" in message


def test_set_budget_accepts_enabled_at_min_boundary(tmp_path: Path) -> None:
    """``total == MIN_ENABLED_BUDGET_USD`` is the inclusive lower bound."""
    from agentshore.budget import MIN_ENABLED_BUDGET_USD

    yaml_path = tmp_path / "agentshore.yaml"
    project_rpc.select(str(tmp_path))

    result = project_rpc.set_budget({"enabled": True, "total": MIN_ENABLED_BUDGET_USD})

    budget = cast("dict[str, object]", result["budget"])
    assert budget["enabled"] is True
    assert budget["total"] == MIN_ENABLED_BUDGET_USD
    assert yaml_path.exists()


def test_set_budget_accepts_disabled_at_zero(tmp_path: Path) -> None:
    """``enabled=False`` + ``total=0`` is always allowed (disabled budget)."""
    yaml_path = tmp_path / "agentshore.yaml"
    project_rpc.select(str(tmp_path))

    result = project_rpc.set_budget({"enabled": False, "total": 0})

    budget = cast("dict[str, object]", result["budget"])
    assert budget["enabled"] is False
    assert budget["total"] == 0.0
    assert yaml_path.exists()


def test_set_budget_rejection_below_min_does_not_modify_yaml(tmp_path: Path) -> None:
    """A rejected enabled-below-min payload must NOT touch agentshore.yaml."""
    yaml_path = tmp_path / "agentshore.yaml"
    initial = "project:\n  path: .\nbudget:\n  enabled: false\n  total: 0.0\n"
    yaml_path.write_text(initial)
    project_rpc.select(str(tmp_path))

    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget({"enabled": True, "total": 5})

    # YAML untouched — atomic-write must not have run.
    assert yaml_path.read_text() == initial


def test_set_budget_rejection_below_min_does_not_create_yaml(tmp_path: Path) -> None:
    """A rejected payload must not create agentshore.yaml when absent."""
    yaml_path = tmp_path / "agentshore.yaml"
    assert not yaml_path.exists()
    project_rpc.select(str(tmp_path))

    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget({"enabled": True, "total": 10})

    assert not yaml_path.exists()


def test_set_budget_rejects_nan_total(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget({"enabled": True, "total": float("nan")})


def test_set_budget_rejects_infinite_total(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget({"enabled": True, "total": float("inf")})


def test_set_budget_rejects_non_numeric_total(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget({"enabled": True, "total": "250"})


def test_set_budget_rejects_non_bool_enabled(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget({"enabled": "yes", "total": 50})


def test_set_budget_rejects_bool_total(tmp_path: Path) -> None:
    """``True`` is an ``int`` subclass — reject it as a dollar amount."""
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget({"enabled": True, "total": True})


def test_set_budget_rejects_extra_fields(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError) as info:
        project_rpc.set_budget({"enabled": True, "total": 50, "stowaway": 1})
    assert "unknown" in str(info.value).lower()


def test_set_budget_rejects_missing_enabled(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget({"total": 50})


def test_set_budget_rejects_missing_total(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget({"enabled": True})


def test_set_budget_rejects_non_object_payload(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget("not a dict")


def test_set_budget_rejects_warning_threshold_out_of_range(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget({"enabled": True, "total": 50, "warning_threshold": 1.5})


def test_set_budget_round_trips_through_config_loader(tmp_path: Path) -> None:
    """Sidecar-written budget keys must deserialise into BudgetConfig."""
    from agentshore.config import load_config

    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n")
    project_rpc.select(str(tmp_path))

    project_rpc.set_budget({"enabled": True, "total": 175.0, "warning_threshold": 0.25})

    cfg = load_config(yaml_path)
    assert cfg.budget.enabled is True
    assert cfg.budget.total == 175.0
    assert cfg.budget.warning_threshold == 0.25


def test_set_budget_writes_time_cap(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    project_rpc.select(str(tmp_path))

    result = project_rpc.set_budget(
        {"enabled": True, "total": 100.0, "time_enabled": True, "time_total_minutes": 1440}
    )

    budget = cast("dict[str, object]", result["budget"])
    assert budget["time_enabled"] is True
    assert budget["time_total_minutes"] == 1440
    text = yaml_path.read_text()
    assert "time_enabled: true" in text
    assert "time_total_minutes: 1440" in text


def test_set_budget_time_round_trips_through_config_loader(tmp_path: Path) -> None:
    from agentshore.config import load_config

    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n")
    project_rpc.select(str(tmp_path))

    project_rpc.set_budget(
        {"enabled": False, "total": 0, "time_enabled": True, "time_total_minutes": 720}
    )

    cfg = load_config(yaml_path)
    assert cfg.budget.time_enabled is True
    assert cfg.budget.time_total_minutes == 720


@pytest.mark.parametrize("minutes", [59, 4321, 30])
def test_set_budget_rejects_time_out_of_range(tmp_path: Path, minutes: int) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError) as info:
        project_rpc.set_budget(
            {"enabled": False, "total": 0, "time_enabled": True, "time_total_minutes": minutes}
        )
    assert "time_total_minutes" in str(info.value)


def test_set_budget_rejects_non_int_time(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget(
            {"enabled": False, "total": 0, "time_enabled": True, "time_total_minutes": "1440"}
        )


def test_set_budget_rejects_bool_time_total(tmp_path: Path) -> None:
    """``True`` is an ``int`` subclass — reject it as a minutes value."""
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_budget(
            {"enabled": False, "total": 0, "time_enabled": True, "time_total_minutes": True}
        )


# ---------------------------------------------------------------------------
# project.set_trusted_issue_enforcement
# ---------------------------------------------------------------------------


def test_set_trusted_issue_enforcement_writes_block(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n  goals: ship it\n")
    project_rpc.select(str(tmp_path))

    result = project_rpc.set_trusted_issue_enforcement(True)

    assert result["enabled"] is True
    assert result["yaml_path"] == str(yaml_path)
    new_text = yaml_path.read_text()
    assert "trusted_ids:" in new_text
    assert "restrict_issues_to_trusted_authors: true" in new_text
    # Pre-existing keys preserved by ruamel.yaml round-trip.
    assert "path: ." in new_text
    assert "goals: ship it" in new_text


def test_set_trusted_issue_enforcement_creates_yaml_when_missing(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    assert not yaml_path.exists()
    project_rpc.select(str(tmp_path))

    result = project_rpc.set_trusted_issue_enforcement(True)

    assert result["enabled"] is True
    assert yaml_path.exists()
    text = yaml_path.read_text()
    assert "trusted_ids:" in text
    assert "restrict_issues_to_trusted_authors: true" in text


def test_set_trusted_issue_enforcement_preserves_other_top_level_keys(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "project:\n  path: .\n"
        "agents:\n  claude_code:\n    enabled: true\n"
        "trusted_ids:\n  github_logins:\n    - alice\n"
    )
    project_rpc.select(str(tmp_path))

    project_rpc.set_trusted_issue_enforcement(True)

    text = yaml_path.read_text()
    assert "project:" in text
    assert "path: ." in text
    assert "agents:" in text
    assert "claude_code:" in text
    # Existing trusted_ids sub-keys preserved; only the flag is added.
    assert "github_logins:" in text
    assert "- alice" in text
    assert "restrict_issues_to_trusted_authors: true" in text


def test_set_trusted_issue_enforcement_disable_writes_false(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    project_rpc.select(str(tmp_path))

    result = project_rpc.set_trusted_issue_enforcement(False)

    assert result["enabled"] is False
    text = yaml_path.read_text()
    assert "restrict_issues_to_trusted_authors: false" in text


def test_set_trusted_issue_enforcement_rejects_non_bool(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_trusted_issue_enforcement("yes")


def test_set_trusted_issue_enforcement_round_trips_through_config_loader(tmp_path: Path) -> None:
    """Sidecar-written flag must deserialise into TrustedIdsConfig."""
    from agentshore.config import load_config

    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n")
    project_rpc.select(str(tmp_path))

    project_rpc.set_trusted_issue_enforcement(True)

    cfg = load_config(yaml_path)
    assert cfg.trusted_ids.restrict_issues_to_trusted_authors is True


def test_dispatch_set_trusted_issue_enforcement_happy_path(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.set_trusted_issue_enforcement",
            "params": {"enabled": True},
        }
    )
    assert response is not None
    assert "result" in response
    result = cast("dict[str, object]", response["result"])
    assert result["enabled"] is True
    assert result["yaml_path"] == str(tmp_path / "agentshore.yaml")


def test_dispatch_set_trusted_issue_enforcement_without_active_remaps_to_public_code() -> None:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.set_trusted_issue_enforcement",
            "params": {"enabled": True},
        }
    )
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == ERR_NO_ACTIVE_PROJECT


def test_dispatch_set_trusted_issue_enforcement_missing_enabled_is_invalid_params(
    tmp_path: Path,
) -> None:
    project_rpc.select(str(tmp_path))
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.set_trusted_issue_enforcement",
            "params": {},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_set_budget_capabilities_advertised() -> None:
    """``project.set_budget`` must appear in the handshake capabilities list."""
    caps = capabilities()
    assert "project.set_budget" in caps


# ---------------------------------------------------------------------------
# JSON-RPC dispatcher integration
# ---------------------------------------------------------------------------


def test_dispatch_project_select_returns_result_envelope(tmp_path: Path) -> None:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.select",
            "params": {"path": str(tmp_path)},
        }
    )
    assert response is not None
    assert "result" in response
    result = cast("dict[str, object]", response["result"])
    assert result["path"] == str(tmp_path.resolve())


def test_dispatch_project_select_invalid_params() -> None:
    response = handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "project.select", "params": {"path": 42}}
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_dispatch_project_select_missing_params() -> None:
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "project.select"})
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_dispatch_project_select_rejects_switch_while_session_active(tmp_path: Path) -> None:
    old_project = tmp_path / "old"
    new_project = tmp_path / "new"
    old_project.mkdir()
    new_project.mkdir()
    state = ServerState(active_project_path=str(old_project), session_active=True)

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.select",
            "params": {"path": str(new_project)},
        },
        state=state,
    )
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == ERR_SESSION_ACTIVE
    assert state.active_project_path == str(old_project)


def test_dispatch_project_inspect_without_active() -> None:
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "project.inspect"})
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == ERR_NO_ACTIVE_PROJECT


def test_dispatch_project_branches_without_active_remaps_to_public_code() -> None:
    """project.branches must surface ERR_NO_ACTIVE_PROJECT, not internal -32004."""
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "project.branches"})
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == ERR_NO_ACTIVE_PROJECT


def test_dispatch_project_set_target_branch_without_active_remaps_to_public_code() -> None:
    """project.set_target_branch must surface ERR_NO_ACTIVE_PROJECT too."""
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.set_target_branch",
            "params": {"name": "main"},
        }
    )
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == ERR_NO_ACTIVE_PROJECT


def test_dispatch_project_branches_rejects_non_bool_refresh(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.branches",
            "params": {"refresh": "yes"},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_dispatch_project_deselect_returns_empty_object(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "project.deselect"})
    assert response is not None
    assert response["result"] == {}


def test_dispatch_set_target_branch_maps_invalid_yaml_to_agentshore_yaml_error(
    git_repo: Path,
) -> None:
    yaml_path = git_repo / "agentshore.yaml"
    yaml_path.write_text("project: [\n", encoding="utf-8")
    project_rpc.select(str(git_repo))
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.set_target_branch",
            "params": {"name": "main"},
        }
    )
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32003


def test_dispatch_set_budget_happy_path(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.set_budget",
            "params": {"budget": {"enabled": True, "total": 300}},
        }
    )
    assert response is not None
    assert "result" in response
    result = cast("dict[str, object]", response["result"])
    budget = cast("dict[str, object]", result["budget"])
    assert budget["enabled"] is True
    assert budget["total"] == 300.0
    assert budget["warning_threshold"] == 0.20


def test_dispatch_set_budget_without_active_remaps_to_public_code() -> None:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.set_budget",
            "params": {"budget": {"enabled": False, "total": 0}},
        }
    )
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == ERR_NO_ACTIVE_PROJECT


def test_dispatch_set_budget_missing_budget_param_is_invalid_params(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.set_budget",
            "params": {},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_dispatch_set_budget_rejects_negative_total_via_dispatcher(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.set_budget",
            "params": {"budget": {"enabled": True, "total": -10}},
        }
    )
    assert response is not None
    assert "error" in response
    # Validation errors use the project_rpc default code (-32000).
    assert response["error"]["code"] == -32000


def test_dispatch_set_budget_rejects_extra_fields_via_dispatcher(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.set_budget",
            "params": {
                "budget": {"enabled": True, "total": 50, "stowaway": 1},
            },
        }
    )
    assert response is not None
    assert "error" in response
    assert "unknown" in response["error"]["message"].lower()


def test_dispatch_project_notification_returns_none(tmp_path: Path) -> None:
    # Matches the existing app.handshake convention: notifications are
    # dropped without running side-effects. The desktop shell always sends
    # ids for project.* calls because it needs the result.
    response = handle_request(
        {"jsonrpc": "2.0", "method": "project.select", "params": {"path": str(tmp_path)}}
    )
    assert response is None
    assert project_rpc.current() is None


def test_unknown_project_method_falls_through_to_method_not_found() -> None:
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "project.bogus"})
    assert response is not None
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_request_payload_is_json_serialisable(git_repo: Path) -> None:
    project_rpc.select(str(git_repo))
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "project.inspect"})
    json.dumps(response)


# ---------------------------------------------------------------------------
# project.select — DESIGN §1.3 side-effects
# ---------------------------------------------------------------------------


def test_dispatch_project_select_response_includes_inspect_envelope(git_repo: Path) -> None:
    """§1.3 requires re-running ``project.inspect`` on select; return it inline."""
    response = _drive(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.select",
            "params": {"path": str(git_repo)},
        }
    )
    assert "result" in response
    result = cast("dict[str, object]", response["result"])
    assert result["path"] == str(git_repo.resolve())
    inspect_payload = cast("dict[str, object]", result["inspect"])
    assert inspect_payload["path"] == str(git_repo.resolve())
    identity = cast("dict[str, object]", inspect_payload["repo_identity"])
    assert identity["is_git"] is True
    # ``inspect`` must reflect the just-selected project, not any prior slot.
    assert inspect_payload["branch"] == "main"


def test_dispatch_project_select_can_skip_inspect_for_desktop_fast_path(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chooser can bind the active path without waiting on readiness probes."""

    def fail_inspect() -> dict[str, object]:
        raise AssertionError("project.inspect should not run")

    monkeypatch.setattr(project_rpc, "inspect", fail_inspect)

    response = _drive(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.select",
            "params": {"path": str(git_repo), "include_inspect": False},
        }
    )
    assert "result" in response
    result = cast("dict[str, object]", response["result"])
    assert result == {"path": str(git_repo.resolve())}


def test_dispatch_project_select_rejects_non_bool_include_inspect(tmp_path: Path) -> None:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.select",
            "params": {"path": str(tmp_path), "include_inspect": "no"},
        }
    )
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == INVALID_PARAMS


def test_dispatch_project_select_switch_closes_existing_data_store(tmp_path: Path) -> None:
    """§1.3: switching to a different project closes open DB handles."""
    old_project = tmp_path / "old"
    new_project = tmp_path / "new"
    old_project.mkdir()
    new_project.mkdir()
    store = _StubStore()
    state = ServerState(
        active_project_path=str(old_project.resolve()),
        data_store=cast("object", store),
    )

    response = _drive(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.select",
            "params": {"path": str(new_project)},
        },
        state=state,
    )
    assert "result" in response
    assert store.closed is True
    assert state.data_store is None
    assert state.active_project_path == str(new_project.resolve())


def test_dispatch_project_select_idempotent_keeps_data_store(tmp_path: Path) -> None:
    """Same-path select must not close DB handles (§1.3 reuse, not switch)."""
    store = _StubStore()
    state = ServerState(
        active_project_path=str(tmp_path.resolve()),
        data_store=cast("object", store),
    )
    response = _drive(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.select",
            "params": {"path": str(tmp_path)},
        },
        state=state,
    )
    assert "result" in response
    assert store.closed is False
    assert state.data_store is store
    assert state.active_project_path == str(tmp_path.resolve())


def test_dispatch_project_select_session_active_preserves_data_store(tmp_path: Path) -> None:
    """ERR_SESSION_ACTIVE must short-circuit before any DB close (§1.3)."""
    old_project = tmp_path / "old"
    new_project = tmp_path / "new"
    old_project.mkdir()
    new_project.mkdir()
    store = _StubStore()
    state = ServerState(
        active_project_path=str(old_project),
        session_active=True,
        data_store=cast("object", store),
    )

    response = _drive(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "project.select",
            "params": {"path": str(new_project)},
        },
        state=state,
    )
    assert "error" in response
    assert response["error"]["code"] == ERR_SESSION_ACTIVE
    assert store.closed is False
    assert state.data_store is store
    assert state.active_project_path == str(old_project)


# ---------------------------------------------------------------------------
# project.set_seed_paths (intake.seed_paths persistence)
# ---------------------------------------------------------------------------


def test_set_seed_paths_writes_yaml(git_repo: Path) -> None:
    (git_repo / "PRD.md").write_text("# seed\n")
    yaml_path = git_repo / "agentshore.yaml"
    yaml_path.write_text('intake:\n  label_prefix: "agentshore/"\n')
    project_rpc.select(str(git_repo))

    result = project_rpc.set_seed_paths(["PRD.md"])
    assert result["seed_paths"] == ["PRD.md"]

    new_text = yaml_path.read_text()
    assert "seed_paths" in new_text
    assert "PRD.md" in new_text
    # Pre-existing intake key preserved (ruamel round-trip).
    assert "label_prefix" in new_text


def test_set_seed_paths_accepts_single_string(git_repo: Path) -> None:
    (git_repo / "PRD.md").write_text("# seed\n")
    project_rpc.select(str(git_repo))

    result = project_rpc.set_seed_paths("PRD.md")
    assert result["seed_paths"] == ["PRD.md"]


def test_set_seed_paths_empty_list_clears(git_repo: Path) -> None:
    yaml_path = git_repo / "agentshore.yaml"
    yaml_path.write_text("intake:\n  seed_paths:\n    - old.md\n")
    project_rpc.select(str(git_repo))

    project_rpc.set_seed_paths([])
    assert "old.md" not in yaml_path.read_text()


def test_set_seed_paths_rejects_missing_path(git_repo: Path) -> None:
    project_rpc.select(str(git_repo))
    with pytest.raises(project_rpc.ProjectError) as info:
        project_rpc.set_seed_paths(["nope.md"])
    assert info.value.code == -32002


def test_set_seed_paths_rejects_non_string_entry(git_repo: Path) -> None:
    project_rpc.select(str(git_repo))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_seed_paths([123])


# ---------------------------------------------------------------------------
# project.set_timelapse / project.install_timelapse (timelapse feature)
# ---------------------------------------------------------------------------


def test_set_timelapse_writes_block(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n  goals: ship it\n")
    project_rpc.select(str(tmp_path))

    result = project_rpc.set_timelapse({"enabled": True, "installed": True})

    timelapse = cast("dict[str, object]", result["timelapse"])
    assert timelapse["enabled"] is True
    assert timelapse["installed"] is True
    text = yaml_path.read_text()
    assert "timelapse:" in text
    assert "enabled: true" in text
    assert "installed: true" in text
    # Pre-existing keys preserved by ruamel.yaml round-trip.
    assert "path: ." in text
    assert "goals: ship it" in text


def test_set_timelapse_defaults_false(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    result = project_rpc.set_timelapse({})
    timelapse = cast("dict[str, object]", result["timelapse"])
    assert timelapse["enabled"] is False
    assert timelapse["installed"] is False


def test_set_timelapse_rejects_non_bool(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_timelapse({"enabled": "yes"})


def test_set_timelapse_rejects_unknown_key(tmp_path: Path) -> None:
    project_rpc.select(str(tmp_path))
    with pytest.raises(project_rpc.ProjectError):
        project_rpc.set_timelapse({"fps": 24})


def test_set_timelapse_preserves_other_keys(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("budget:\n  enabled: false\n  total: 0.0\n")
    project_rpc.select(str(tmp_path))

    project_rpc.set_timelapse({"enabled": True, "installed": True})

    text = yaml_path.read_text()
    assert "budget:" in text
    assert "timelapse:" in text


def test_install_timelapse_persists_installed_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentshore.timelapse.setup import InstallResult

    async def _fake_install(cwd: Path | None = None) -> InstallResult:
        return InstallResult(success=True, message="ok")

    monkeypatch.setattr("agentshore.timelapse.setup.install_timelapse", _fake_install)
    yaml_path = tmp_path / "agentshore.yaml"
    project_rpc.select(str(tmp_path))

    result = asyncio.run(project_rpc.install_timelapse())

    assert result["success"] is True
    assert result["installed"] is True
    assert "installed: true" in yaml_path.read_text()


def test_install_timelapse_failure_does_not_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentshore.timelapse.setup import InstallResult

    async def _fake_install(cwd: Path | None = None) -> InstallResult:
        return InstallResult(success=False, message="brew missing")

    monkeypatch.setattr("agentshore.timelapse.setup.install_timelapse", _fake_install)
    yaml_path = tmp_path / "agentshore.yaml"
    project_rpc.select(str(tmp_path))

    result = asyncio.run(project_rpc.install_timelapse())

    assert result["success"] is False
    assert result["installed"] is False
    assert result["message"] == "brew missing"
    assert not yaml_path.exists()
