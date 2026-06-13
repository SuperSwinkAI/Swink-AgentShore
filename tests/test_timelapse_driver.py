"""Unit tests for the timelapse-capture driver (start/stop/await_output).

``run_command`` is patched so no real ``timelapse-capture`` process runs; the
tests assert JSON parsing of ``alias``/``runDir``/``outputPath`` and that the
run-id is reused on stop/status.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentshore import timelapse
from agentshore.command import CommandResult


def _result(
    args: tuple[str, ...], *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> CommandResult:
    return CommandResult(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _force_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pretend the CLI is installed so resolve_timelapse_binary() returns a path.
    monkeypatch.setattr(timelapse, "resolve_timelapse_binary", lambda: "timelapse-capture")


async def test_start_capture_parses_alias_and_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def fake_run(*args: str, **kwargs: object) -> CommandResult:
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        payload = {"alias": "swift-otter-042", "runDir": str(tmp_path / "timelapse-runs" / "x")}
        return _result(args, stdout=json.dumps(payload))

    monkeypatch.setattr(timelapse, "run_command", fake_run)

    run = await timelapse.start_capture("http://localhost:9400/", tmp_path)

    assert run.run_id == "swift-otter-042"
    # run_dir echoes the tool's native path (backslashes on Windows); assert the
    # final component rather than a hardcoded forward-slash suffix.
    assert Path(run.run_dir).name == "x"
    # start uses the default runs dir (no --out) so the alias resolves later.
    assert "--out" not in captured["args"]
    assert "start" in captured["args"]
    assert captured["cwd"] == tmp_path


async def test_start_capture_raises_on_missing_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run(*args: str, **kwargs: object) -> CommandResult:
        return _result(args, stdout=json.dumps({"runDir": "/x"}))

    monkeypatch.setattr(timelapse, "run_command", fake_run)

    with pytest.raises(timelapse.TimelapseError):
        await timelapse.start_capture("http://localhost/", tmp_path)


async def test_start_capture_raises_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run(*args: str, **kwargs: object) -> CommandResult:
        return _result(args, returncode=1, stderr="boom")

    monkeypatch.setattr(timelapse, "run_command", fake_run)

    with pytest.raises(timelapse.TimelapseError, match="boom"):
        await timelapse.start_capture("http://localhost/", tmp_path)


async def test_stop_capture_passes_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def fake_run(*args: str, **kwargs: object) -> CommandResult:
        seen["args"] = args
        return _result(args, stdout="{}")

    monkeypatch.setattr(timelapse, "run_command", fake_run)

    await timelapse.stop_capture("swift-otter-042", tmp_path)

    assert "stop" in seen["args"]
    assert "swift-otter-042" in seen["args"]


async def test_await_output_returns_path_once_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    async def fake_run(*args: str, **kwargs: object) -> CommandResult:
        calls["n"] += 1
        # First poll: still rendering (no outputPath). Second: done.
        if calls["n"] < 2:
            return _result(args, stdout=json.dumps({"state": "rendering", "outputPath": None}))
        return _result(
            args, stdout=json.dumps({"state": "done", "outputPath": "/runs/x/output.mp4"})
        )

    monkeypatch.setattr(timelapse, "run_command", fake_run)

    out = await timelapse.await_output(
        "swift-otter-042", tmp_path, max_polls=5, poll_interval_seconds=0
    )

    assert out == "/runs/x/output.mp4"
    assert calls["n"] == 2


async def test_await_output_reads_nested_status_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """timelapse-capture >=0.3.1 nests the run state under a ``status`` key.

    Regression: the driver read a flat top-level ``outputPath`` that 0.3.1 no
    longer emits, so await_output polled to timeout and the ESR opened nothing.
    """
    calls = {"n": 0}

    async def fake_run(*args: str, **kwargs: object) -> CommandResult:
        calls["n"] += 1
        if calls["n"] < 2:
            return _result(args, stdout=json.dumps({"status": {"state": "rendering"}}))
        return _result(
            args,
            stdout=json.dumps(
                {"status": {"state": "rendered", "outputPath": "/runs/x/output.mp4"}}
            ),
        )

    monkeypatch.setattr(timelapse, "run_command", fake_run)

    out = await timelapse.await_output(
        "plucky-sparrow-523", tmp_path, max_polls=5, poll_interval_seconds=0
    )

    assert out == "/runs/x/output.mp4"
    assert calls["n"] == 2


async def test_await_output_returns_none_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run(*args: str, **kwargs: object) -> CommandResult:
        return _result(args, stdout=json.dumps({"state": "rendering", "outputPath": None}))

    monkeypatch.setattr(timelapse, "run_command", fake_run)

    out = await timelapse.await_output(
        "swift-otter-042", tmp_path, max_polls=3, poll_interval_seconds=0
    )

    assert out is None
