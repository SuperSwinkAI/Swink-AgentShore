"""End-to-end natural-exit test for ``session.completed`` (gh-385).

DESIGN §5.2: when the orchestrator exits without an explicit
``session.stop`` (drain_complete, max_plays, timeout, shutting_down),
the sidecar must emit a ``session.completed`` JSON-RPC notification on
stdio with the same payload shape that ``session.stop`` returns.

This test drives a real Orchestrator subprocess (no mocks) to natural
completion via ``session.max_plays=0`` — every tick sees
``total_plays >= max_plays`` and ``_should_terminate`` returns
``(True, "max_plays")``. The supervisor task then runs ``orch.stop()``,
builds an ESR payload, and the notification emitter fires
``session.completed``. We read stdio for that notification and assert
parity with the keys ``session.stop`` returns.

POSIX-only because the existing sidecar subprocess tests are; the
``ps``-based budget tests are guarded the same way.
"""

from __future__ import annotations

import json
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

NATURAL_EXIT_TIMEOUT_SECONDS = 180.0

# session.stop's ESR response carries (at minimum) these keys; the
# natural-exit emitter uses build_esr_payload so the same shape is
# expected. archive_path/report_path/log_path are paths that may not exist on
# disk in this minimal-project case, so we don't assert their content,
# only that the keys are present.
EXPECTED_ESR_KEYS = frozenset(
    {
        "session_id",
        "exit_reason",
        "exit_code",
        "archive_path",
        "report_path",
        "log_path",
    }
)

VALID_TIERED_CONFIG = """\
identities:
  alpha:
    git_user_name: Alpha
    git_user_email: alpha@example.com
    gh_token_login: alpha
  beta:
    git_user_name: Beta
    git_user_email: beta@example.com
    gh_token_login: beta
agents:
  claude_code:
    enabled: true
    binary: agentshore-missing-claude
    identity: alpha
    model_tiers:
      small:
        enabled: true
      medium:
        enabled: true
  codex:
    enabled: true
    binary: agentshore-missing-codex
    identity: beta
    model_tiers:
      large:
        enabled: true
"""

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("ps") is None,
    reason="sidecar subprocess tests are POSIX-only (select() on a pipe fails on "
    "Windows with WSAENOTSOCK); mirrors test_nfr_budgets",
)


def _sidecar_command() -> list[str]:
    return [sys.executable, "-m", "agentshore.sidecar"]


def _send(child: subprocess.Popen[str], req_id: int, method: str, params: object = None) -> None:
    assert child.stdin is not None
    request: dict[str, object] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        request["params"] = params
    child.stdin.write(json.dumps(request) + "\n")
    child.stdin.flush()


def _readline_with_deadline(child: subprocess.Popen[str], deadline: float) -> str | None:
    """``readline()`` that honors a wall-clock deadline.

    Returns the next line of stdout, or ``None`` when the deadline
    elapses with no line ready. We poll with ``select`` on the file
    descriptor so we don't block past the deadline waiting for input
    that may never arrive (the natural-exit path can take longer than
    the test deadline if bootstrap is slow, and a plain blocking
    ``readline()`` would hide the timeout).
    """
    assert child.stdout is not None
    fd = child.stdout.fileno()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # Cap the per-poll wait so test cancellation stays responsive.
        ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
        if ready:
            line = child.stdout.readline()
            if line == "":
                # EOF — sidecar exited.
                return None
            return line
        if child.poll() is not None:
            # Sidecar died without writing anything more.
            return None


def _read_response(
    child: subprocess.Popen[str], req_id: int, *, deadline: float
) -> dict[str, object]:
    """Read lines until a JSON-RPC response with ``id == req_id`` arrives.

    Notifications (no ``id``) are dropped — callers wanting to inspect
    notifications should use :func:`_read_until_notification`.
    """
    while True:
        line = _readline_with_deadline(child, deadline)
        if line is None:
            return {}
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == req_id:
            return payload  # type: ignore[no-any-return]


def _read_until_notification(
    child: subprocess.Popen[str],
    method: str,
    *,
    deadline: float,
) -> dict[str, object] | None:
    """Read stdio until a notification with ``method`` arrives, or deadline."""
    while True:
        line = _readline_with_deadline(child, deadline)
        if line is None:
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("method") == method and "id" not in payload:
            return payload  # type: ignore[no-any-return]


def test_natural_exit_emits_session_completed_with_esr_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Boot the orchestrator with ``max_plays=0`` so the first tick
    triggers natural exit, and assert the sidecar emits
    ``session.completed`` with the ESR keys ``session.stop`` returns.
    """
    project_path = tmp_path / "natural-exit-project"
    project_path.mkdir()
    # max_plays=0 → first tick: total_plays=0 >= 0 → terminate with
    # reason="max_plays", which is a natural exit (not "stop_requested").
    (project_path / "agentshore.yaml").write_text(
        f"{VALID_TIERED_CONFIG}session:\n  max_plays: 0\n",
        encoding="utf-8",
    )
    (project_path / ".beads").mkdir()

    child = subprocess.Popen(
        _sidecar_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        # Handshake first so the rest of the methods are accepted.
        from agentshore.sidecar.build_id import load_build_info

        overall_deadline = time.monotonic() + NATURAL_EXIT_TIMEOUT_SECONDS
        _send(
            child,
            1,
            "app.handshake",
            {
                "client": "agentshore-natural-exit-test",
                "client_build_id": load_build_info()["build_id"],
            },
        )
        handshake = _read_response(child, 1, deadline=overall_deadline)
        assert "result" in handshake, handshake

        # project.select binds the cwd-equivalent.
        _send(child, 2, "project.select", {"path": str(project_path)})
        select_resp = _read_response(child, 2, deadline=overall_deadline)
        assert "result" in select_resp, select_resp

        # session.start boots the orchestrator. The handler skips
        # $/progress notifications when reading the response.
        _send(child, 3, "session.start")
        start_resp = _read_response(child, 3, deadline=overall_deadline)
        assert "result" in start_resp, start_resp

        # Now wait for the natural-exit notification.
        notif = _read_until_notification(child, "session.completed", deadline=overall_deadline)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        # Always capture stderr so a failing assert below has context.
        try:
            stderr_output = child.stderr.read() if child.stderr is not None else ""
        except Exception:
            stderr_output = ""

    if notif is None:
        with capsys.disabled():
            print(
                "\n[gh-385] session.completed not received — sidecar stderr tail:\n"
                + "\n".join(stderr_output.splitlines()[-50:])
            )
    assert notif is not None, (
        f"session.completed not received within {NATURAL_EXIT_TIMEOUT_SECONDS}s"
    )
    params = notif.get("params")
    assert isinstance(params, dict), f"session.completed params must be a dict, got {params!r}"
    missing = EXPECTED_ESR_KEYS - set(params.keys())
    assert not missing, f"session.completed payload missing keys: {sorted(missing)}"

    # The exit_reason must be a natural-exit reason (not stop_requested).
    assert params.get("exit_reason") in {
        "max_plays",
        "drain_complete",
        "timeout",
        "shutting_down",
    }, f"unexpected exit_reason: {params.get('exit_reason')!r}"

    with capsys.disabled():
        print(
            f"\n[gh-385] session.completed received with exit_reason="
            f"{params.get('exit_reason')!r}, keys={sorted(params.keys())}"
        )
