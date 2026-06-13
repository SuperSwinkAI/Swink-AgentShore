"""Guard: the macOS build CLI maps flags to BuildContext correctly.

`python -m scripts.buildkit macos` is the build entrypoint, so the flag ->
context contract must hold. Loaded by file path to stay independent of
scripts/ being importable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_REPO = Path(__file__).resolve().parents[2]
_BUILDKIT = _REPO / "scripts" / "buildkit"


def _load_macos() -> ModuleType:
    # Register the package so macos.py's relative imports (`from . import ...`) resolve.
    pkg_spec = importlib.util.spec_from_file_location(
        "scripts_buildkit_pkg2",
        _BUILDKIT / "__init__.py",
        submodule_search_locations=[str(_BUILDKIT)],
    )
    assert pkg_spec is not None and pkg_spec.loader is not None
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules["scripts_buildkit_pkg2"] = pkg
    pkg_spec.loader.exec_module(pkg)
    for sub in ("version", "_proc", "context", "phases", "verify", "macos"):
        spec = importlib.util.spec_from_file_location(
            f"scripts_buildkit_pkg2.{sub}", _BUILDKIT / f"{sub}.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"scripts_buildkit_pkg2.{sub}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["scripts_buildkit_pkg2.macos"]


def test_default_flags_full_signed_pkg_build() -> None:
    macos = _load_macos()
    ctx = macos.parse_args([])
    assert ctx.build_mode == "release"
    assert ctx.build_pkg is True
    assert ctx.no_sign is False
    assert ctx.notarize is False
    assert ctx.skip_dashboard is False


def test_all_flags_map_through() -> None:
    macos = _load_macos()
    ctx = macos.parse_args(
        ["--debug", "--no-pkg", "--no-sign", "--skip-dashboard", "--skip-sidecar", "--install"]
    )
    assert ctx.build_mode == "debug"
    assert ctx.build_pkg is False
    assert ctx.no_sign is True
    assert ctx.skip_dashboard is True
    assert ctx.skip_sidecar is True
    assert ctx.do_install is True


def test_notarize_with_no_pkg_is_rejected() -> None:
    macos = _load_macos()
    ctx = macos.parse_args(["--notarize", "--no-pkg"])
    import pytest

    with pytest.raises(macos.BuildError):
        macos.run_macos(ctx)
