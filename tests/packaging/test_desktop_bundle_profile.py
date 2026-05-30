from __future__ import annotations

import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"


def _load() -> dict:  # type: ignore[type-arg]
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def test_cli_dependencies_still_include_trimmed_packages() -> None:
    data = _load()
    base_deps = data["project"]["dependencies"]
    for pkg in ("textual", "beaupy", "playwright"):
        assert any(dep.startswith(pkg) for dep in base_deps), (
            f"{pkg} missing from [project] dependencies"
        )
