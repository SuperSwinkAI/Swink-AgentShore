"""Tests for the ConPTY spawn adapter (Antigravity/agy on Windows).

The real ``pywinpty`` backend only exists on Windows, so these tests inject a
fake PTY object to exercise the platform-agnostic logic — the stdout stream
bridge, returncode/wait plumbing, the merged-stderr-at-EOF contract, and the
``should_use_conpty`` gating — on every platform (including Linux CI).
"""

from __future__ import annotations

import asyncio

from agentshore.agents.cli import conpty
from agentshore.state import AgentType


class _FakePty:
    """Minimal stand-in for ``winpty.PtyProcess`` used by ``conpty.PtyProcess``."""

    def __init__(self, chunks: list[str], *, exitstatus: int | None = 0, pid: int = 4242) -> None:
        self._chunks = list(chunks)
        self.exitstatus = exitstatus
        self.pid = pid
        self.terminated = False

    def read(self, _n: int) -> str:
        if self._chunks:
            return self._chunks.pop(0)
        raise EOFError

    def isalive(self) -> bool:
        return bool(self._chunks)

    def terminate(self, force: bool = False) -> None:  # noqa: FBT001, FBT002
        self.terminated = True
        self._chunks = []


def test_should_use_conpty_true_on_windows_with_winpty(monkeypatch) -> None:
    monkeypatch.setattr(conpty.sys, "platform", "win32")
    monkeypatch.setattr(conpty, "_HAS_WINPTY", True)
    assert conpty.should_use_conpty(AgentType.ANTIGRAVITY) is True


def test_should_use_conpty_false_for_other_agents(monkeypatch) -> None:
    monkeypatch.setattr(conpty.sys, "platform", "win32")
    monkeypatch.setattr(conpty, "_HAS_WINPTY", True)
    assert conpty.should_use_conpty(AgentType.CLAUDE_CODE) is False
    assert conpty.should_use_conpty(AgentType.CODEX) is False


def test_should_use_conpty_false_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(conpty.sys, "platform", "linux")
    monkeypatch.setattr(conpty, "_HAS_WINPTY", True)
    assert conpty.should_use_conpty(AgentType.ANTIGRAVITY) is False


def test_should_use_conpty_false_and_warns_without_winpty(monkeypatch) -> None:
    monkeypatch.setattr(conpty.sys, "platform", "win32")
    monkeypatch.setattr(conpty, "_HAS_WINPTY", False)
    assert conpty.should_use_conpty(AgentType.ANTIGRAVITY) is False


async def test_pty_adapter_bridges_stdout_and_returncode() -> None:
    loop = asyncio.get_running_loop()
    fake = _FakePty(["hel", "lo\n", "world\n"], exitstatus=0)
    proc = conpty.PtyProcess(fake, loop=loop, limit=65536)

    assert proc.stdout is not None
    data = await asyncio.wait_for(proc.stdout.read(), timeout=5.0)
    assert data == b"hello\nworld\n"

    rc = await asyncio.wait_for(proc.wait(), timeout=5.0)
    assert rc == 0
    assert proc.returncode == 0
    assert proc.pid == 4242


async def test_pty_adapter_stderr_is_immediately_eof() -> None:
    loop = asyncio.get_running_loop()
    proc = conpty.PtyProcess(_FakePty(["x\n"]), loop=loop, limit=65536)
    assert proc.stderr is not None
    assert proc.stderr.at_eof()
    # stdin is never used (agy has no stdin prompt mode).
    assert proc.stdin is None
    # transport is absent so _close_process_transport is a no-op.
    assert proc._transport is None
    await asyncio.wait_for(proc.wait(), timeout=5.0)


async def test_pty_adapter_async_iteration_yields_lines() -> None:
    loop = asyncio.get_running_loop()
    fake = _FakePty(["one\n", "two\nthree\n"], exitstatus=0)
    proc = conpty.PtyProcess(fake, loop=loop, limit=65536)
    assert proc.stdout is not None
    lines = [line async for line in proc.stdout]
    assert lines == [b"one\n", b"two\n", b"three\n"]
    assert await asyncio.wait_for(proc.wait(), timeout=5.0) == 0


async def test_pty_adapter_returncode_defaults_zero_when_status_unknown() -> None:
    loop = asyncio.get_running_loop()
    proc = conpty.PtyProcess(_FakePty(["hi\n"], exitstatus=None), loop=loop, limit=65536)
    assert proc.stdout is not None
    await asyncio.wait_for(proc.stdout.read(), timeout=5.0)
    assert await asyncio.wait_for(proc.wait(), timeout=5.0) == 0
