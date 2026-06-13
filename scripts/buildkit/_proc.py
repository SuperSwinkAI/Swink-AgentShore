"""Logging + subprocess helpers shared by every build phase.

Mirrors the `log()/info()/die()` banners of the original shell scripts so the
build output looks identical, and centralises subprocess invocation so failures
abort loudly (no pipe-swallowed exit codes — the class of bug where a build
"passed" because `tee | tail` returned 0).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

_BOLD = "\033[1m"
_RESET = "\033[0m"


class BuildError(RuntimeError):
    """A build phase failed. Carries a human-readable message; aborts the build."""


def log(message: str) -> None:
    print(f"\n{_BOLD}==> {message}{_RESET}", flush=True)


def info(message: str) -> None:
    print(f"    {message}", flush=True)


def die(message: str) -> BuildError:
    """Return a BuildError to raise — `raise die('...')` reads naturally."""
    return BuildError(message)


def require_tool(name: str, hint: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise die(f"{name} not found — {hint}")
    return path


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> int:
    """Run a command, streaming its output. Raises BuildError on non-zero exit."""
    result = subprocess.run(list(cmd), cwd=cwd, env=dict(env) if env else None)
    if check and result.returncode != 0:
        raise die(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result.returncode


def run_text(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> str:
    """Run a command and capture stdout (stderr passed through). Returns stdout."""
    result = subprocess.run(
        list(cmd),
        cwd=cwd,
        env=dict(env) if env else None,
        stdout=subprocess.PIPE,
        text=True,
    )
    if check and result.returncode != 0:
        raise die(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result.stdout


def run_ok(cmd: Sequence[str], *, cwd: Path | None = None) -> bool:
    """Run a command quietly, returning True on exit 0 (for best-effort steps)."""
    return (
        subprocess.run(
            list(cmd),
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def fatal(error: BuildError) -> int:
    """Print a build error to stderr and return the process exit code."""
    print(f"error: {error}", file=sys.stderr)
    return 1
