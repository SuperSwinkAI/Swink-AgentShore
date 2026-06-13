"""Cross-platform build phases shared by the macOS and Windows pipelines.

These are the phases that were duplicated (and drifted) between
build-macos.sh and build-windows.ps1. Keeping them here is the point of the
spine: one implementation, no drift.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from ._proc import die, info, log, require_tool, run

if TYPE_CHECKING:
    from .context import BuildContext


def _npm() -> str:
    # shutil.which resolves npm.cmd on Windows / npm on POSIX.
    return require_tool("npm", "install Node.js (npm) and retry")


def build_dashboard(ctx: BuildContext) -> None:
    """Dashboard bridge static + lib bundle (build:lib must run second; see desktop-rbn)."""
    if ctx.skip_dashboard:
        log("Skipping dashboard build (--skip-dashboard)")
        return
    npm = _npm()
    dashboard = ctx.root / "dashboard"
    log("Building dashboard bridge static")
    run([npm, "run", "build"], cwd=dashboard)
    log("Building dashboard lib bundle (dist/)")
    run([npm, "run", "build:lib"], cwd=dashboard)


def build_frontend(ctx: BuildContext) -> None:
    log("Building Tauri frontend")
    run([_npm(), "run", "build:tauri-frontend"], cwd=ctx.desktop_dir)


def build_wheel(ctx: BuildContext) -> None:
    """Build the agentshore wheel shipped inside the installer; sets ctx.bundled_wheel."""
    log("Building agentshore python wheel")
    uv = require_tool("uv", "install uv (https://docs.astral.sh/uv/) and retry")
    stage = ctx.tauri_dir / "target" / "agentshore-wheel"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    run([uv, "build", "--wheel", "--out-dir", str(stage)], cwd=ctx.root)
    wheels = sorted(
        stage.glob("agentshore-*-py3-none-any.whl"), key=lambda p: p.stat().st_mtime
    )
    if not wheels:
        raise die(f"uv build did not produce a wheel under {stage}")
    ctx.bundled_wheel = wheels[-1]
    info(f"Wheel: {ctx.bundled_wheel.name}")
