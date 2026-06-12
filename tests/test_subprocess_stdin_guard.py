"""AST guard: every raw process spawn in ``src/agentshore`` must pin stdin.

Bug class this prevents (regressed in 2539686, fixed in b0f552d):
On Windows, the desktop sidecar's stdin **is** the live Tauri JSON-RPC pipe
(continuously drained by the reader thread). Any ``subprocess.run`` /
``Popen`` / ``call`` / ``check_output`` / ``check_call`` — or the async
``asyncio.create_subprocess_exec`` / ``create_subprocess_shell`` — in
sidecar-reachable code that does NOT explicitly set ``stdin=`` inherits that
pipe. git's MSYS2 runtime (and potentially other tools) probe stdin on startup
and then wedge at 0 CPU forever on the contended pipe, hanging session startup.

This guard walks every module under ``src/agentshore`` and fails if any raw
spawn omits BOTH ``stdin=`` and ``input=``. ``input=`` is accepted because
``subprocess.run`` wires the child's stdin itself when an ``input`` payload is
supplied (so the inherited pipe is never touched). The canonical safe value for
everything else is ``stdin=subprocess.DEVNULL`` (or ``asyncio.subprocess.DEVNULL``).

No call sites are allowlisted: the predicate (``stdin=`` OR ``input=``) already
admits every legitimately-safe site, including the wrapper in
``agentshore/command.py`` and the ``input=``-fed loads in
``agentshore/data/integrity.py`` and ``agentshore/keyring_child.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Repo layout: <root>/tests/<this file>, source under <root>/src/agentshore.
SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "agentshore"

# ``subprocess`` spawn primitives that inherit the parent's stdin unless told
# otherwise. ``subprocess.run`` covers run()/call()/check_call()/check_output()
# at runtime, but each may also be referenced directly, so guard them all.
SPAWN_ATTRS = frozenset({"run", "Popen", "call", "check_call", "check_output"})

# Async spawn primitives (``asyncio.create_subprocess_exec`` / ``_shell``).
# Their names are asyncio-specific and unambiguous, so we match on the attribute
# name regardless of receiver (``asyncio.`` vs ``asyncio.subprocess.``). They
# inherit stdin identically and accept the same ``stdin=`` keyword.
ASYNC_SPAWN_ATTRS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})

# Keywords that make a call safe: ``stdin=`` pins the child's stdin explicitly;
# ``input=`` causes subprocess.run to wire stdin itself.
SAFE_KWARGS = frozenset({"stdin", "input"})


def _source_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _is_subprocess_spawn(func: ast.expr) -> bool:
    """True for ``subprocess.<spawn>(...)`` or ``asyncio.create_subprocess_*(...)``."""
    if not isinstance(func, ast.Attribute):
        return False
    # Async spawns: distinctive attribute name, any receiver.
    if func.attr in ASYNC_SPAWN_ATTRS:
        return True
    # Sync spawns: ``subprocess.<attr>`` only (``run`` etc. are too generic to
    # match on the bare attribute name).
    return (
        func.attr in SPAWN_ATTRS
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def _find_offenders(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, attr) for spawn calls missing an explicit stdin/input."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_subprocess_spawn(node.func):
            continue
        keywords = {kw.arg for kw in node.keywords if kw.arg is not None}
        # A ``**kwargs`` splat (kw.arg is None) could carry stdin; treat the
        # presence of any double-star as opaque-but-safe rather than guess.
        has_splat = any(kw.arg is None for kw in node.keywords)
        if has_splat or keywords & SAFE_KWARGS:
            continue
        assert isinstance(node.func, ast.Attribute)
        offenders.append((node.lineno, node.func.attr))
    return offenders


def test_subprocess_calls_pin_stdin() -> None:
    """Every raw subprocess spawn under src/agentshore pins stdin (or input)."""
    failures: list[str] = []
    for path in _source_files():
        for lineno, attr in _find_offenders(path):
            failures.append(f"{path}:{lineno}: {attr}(...)")

    if failures:
        joined = "\n".join(f"  - {f}" for f in failures)
        pytest.fail(
            "Raw process spawn(s) missing an explicit `stdin=` keyword:\n"
            f"{joined}\n\n"
            "On Windows the desktop sidecar's stdin is the live Tauri JSON-RPC "
            "pipe; a child that inherits it (e.g. git's MSYS2 runtime) wedges "
            "session startup forever (regressed in 2539686, fixed in b0f552d).\n"
            "Fix: add `stdin=subprocess.DEVNULL` (or `asyncio.subprocess.DEVNULL`) "
            "to each call above (or pass `input=` if you are feeding the child via "
            "stdin), or route the call through agentshore.command.run_command, "
            "which pins stdin for you."
        )


def test_guard_detects_a_synthetic_offender(tmp_path: Path) -> None:
    """Self-check: the AST predicate flags a missing-stdin call and clears a
    call that pins stdin or feeds input."""
    offender = tmp_path / "bad.py"
    offender.write_text(
        "import subprocess\n"
        "subprocess.run(['git', 'status'])\n"
        "import asyncio\n"
        "asyncio.create_subprocess_exec('ps', stdout=asyncio.subprocess.PIPE)\n",
        encoding="utf-8",
    )
    assert _find_offenders(offender) == [(2, "run"), (4, "create_subprocess_exec")]

    safe = tmp_path / "good.py"
    safe.write_text(
        "import subprocess\n"
        "subprocess.run(['git'], stdin=subprocess.DEVNULL)\n"
        "subprocess.run(['git'], input=b'x')\n"
        "subprocess.Popen(['git'], **kw)\n"
        "asyncio.create_subprocess_exec('ps', stdin=asyncio.subprocess.DEVNULL)\n",
        encoding="utf-8",
    )
    assert _find_offenders(safe) == []
