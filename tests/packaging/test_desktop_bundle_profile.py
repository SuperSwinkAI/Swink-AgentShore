from __future__ import annotations

import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"


def _load() -> dict:  # type: ignore[type-arg]
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def test_cli_dependencies_still_include_trimmed_packages() -> None:
    data = _load()
    base_deps = data["project"]["dependencies"]
    # textual (solo-mode TUI) and beaupy (init wizard) are genuine base CLI deps.
    # playwright was removed: no Python module imports it (timelapse drives the npm
    # `timelapse-capture` CLI via subprocess), so it was ~104 MB of dead weight.
    for pkg in ("textual", "beaupy"):
        assert any(dep.startswith(pkg) for dep in base_deps), (
            f"{pkg} missing from [project] dependencies"
        )
    assert not any(dep.startswith("playwright") for dep in base_deps), (
        "playwright is unused by Python code and must not be a base CLI dependency"
    )
