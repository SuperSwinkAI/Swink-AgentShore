"""Desktop NFR budgets — handshake latency and idle sidecar RSS.

DESIGN §8 (desktop-n7s, gh-181) defines three non-functional baselines:

1. **Handshake < 1s** — time from sending ``app.handshake`` to receiving
   the response over JSON-RPC stdio.
2. **Idle sidecar < 250MB** — resident set size of the sidecar process
   shortly after a successful handshake, with no active session.
3. **Active memory < 1.5GB** — sidecar + tracked agent subprocesses
   during a running session. Deferred until ``session.start`` boots the
   real orchestrator (desktop-0vc.11.2); a follow-up test will cover it.

Each test runs **warning-band** rather than spec-band by default — the
bead's acceptance says baselines are warning-only until CI noise is
characterised. The hard assert uses a 3× headroom so flaky CI runs
don't fail; the recorded measurement is always printed via ``capsys``
so the maintainers can track drift over time and tighten later.

The tests use only stdlib (subprocess, time, shutil) — no new
dependency. POSIX-only (macOS + Linux) because ``ps -o rss=`` is the
portability vehicle; Windows NFR work can land in a follow-up if
needed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentshore.sidecar.build_id import load_build_info

VALID_TIERED_CONFIG = """\
project: {}
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

# Target band from DESIGN §8.
HANDSHAKE_TARGET_MS = 1000.0
IDLE_RSS_TARGET_MB = 250.0
ACTIVE_RSS_TARGET_MB = 1500.0

# Warning band (3× target) — what we hard-assert to keep CI green while
# real-world noise is being characterised. Bump down once we have a
# baseline.
HANDSHAKE_WARN_MS = 3000.0
IDLE_RSS_WARN_MB = 750.0
ACTIVE_RSS_WARN_MB = 4500.0


pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("ps") is None,
    reason="NFR rss read uses POSIX `ps -o rss=`; a git-bash ps on Windows can't "
    "read a native process's RSS, so skip there too",
)


def _sidecar_command() -> list[str]:
    """Launch the sidecar the same way the Rust supervisor does in dev mode."""
    return [sys.executable, "-m", "agentshore.sidecar"]


def _read_rss_kb(pid: int) -> int:
    """Return the sidecar process RSS in kilobytes via ``ps``.

    macOS and Linux both expose ``ps -o rss= -p <pid>`` returning a
    single integer in 1024-byte units. We tolerate transient ``ps``
    failure (process gone) by returning 0 so the caller can decide what
    to do.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return 0
    cleaned = out.strip()
    if not cleaned:
        return 0
    return int(cleaned.split()[0])


def _spawn_sidecar() -> subprocess.Popen[str]:
    return subprocess.Popen(
        _sidecar_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _send_handshake(child: subprocess.Popen[str]) -> tuple[float, dict[str, object]]:
    """Send ``app.handshake`` and return ``(elapsed_ms, parsed_response)``."""
    assert child.stdin is not None and child.stdout is not None
    build_id = load_build_info()["build_id"]
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "app.handshake",
        "params": {"client": "agentshore-desktop-nfr", "client_build_id": build_id},
    }
    line = json.dumps(request) + "\n"

    start = time.perf_counter()
    child.stdin.write(line)
    child.stdin.flush()
    response_line = child.stdout.readline()
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    response = json.loads(response_line)
    return elapsed_ms, response


def test_handshake_latency_under_warning_band(capsys: pytest.CaptureFixture[str]) -> None:
    """``app.handshake`` round-trips within the 3× warning band of §8."""
    child = _spawn_sidecar()
    try:
        elapsed_ms, response = _send_handshake(child)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)

    with capsys.disabled():
        target_marker = "" if elapsed_ms <= HANDSHAKE_TARGET_MS else " [over target]"
        print(
            f"\n[NFR §8] handshake latency: {elapsed_ms:.1f} ms "
            f"(target {HANDSHAKE_TARGET_MS:.0f}, warn {HANDSHAKE_WARN_MS:.0f})"
            f"{target_marker}"
        )

    assert response.get("id") == 1, response
    assert "result" in response, response
    assert elapsed_ms < HANDSHAKE_WARN_MS, (
        f"handshake latency {elapsed_ms:.1f}ms exceeded warning band {HANDSHAKE_WARN_MS:.0f}ms"
    )


def test_idle_sidecar_rss_under_warning_band(capsys: pytest.CaptureFixture[str]) -> None:
    """Sidecar RSS after handshake is within the 3× warning band of §8."""
    child = _spawn_sidecar()
    try:
        _send_handshake(child)
        # Give Python a moment to reach steady-state allocation after the
        # handshake reply. Without this the RSS undercounts because some
        # imports are still resolving.
        time.sleep(0.5)
        rss_kb = _read_rss_kb(child.pid)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)

    assert rss_kb > 0, "ps returned no RSS — process gone before measurement?"
    rss_mb = rss_kb / 1024.0

    with capsys.disabled():
        target_marker = "" if rss_mb <= IDLE_RSS_TARGET_MB else " [over target]"
        print(
            f"\n[NFR §8] idle sidecar RSS: {rss_mb:.1f} MB "
            f"(target {IDLE_RSS_TARGET_MB:.0f}, warn {IDLE_RSS_WARN_MB:.0f})"
            f"{target_marker}"
        )

    assert rss_mb < IDLE_RSS_WARN_MB, (
        f"idle sidecar RSS {rss_mb:.1f}MB exceeded warning band {IDLE_RSS_WARN_MB:.0f}MB"
    )


def _send_rpc(
    child: subprocess.Popen[str], req_id: int, method: str, params: object = None
) -> dict[str, object]:
    """Send a generic JSON-RPC request and return the parsed response."""
    assert child.stdin is not None and child.stdout is not None
    request: dict[str, object] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        request["params"] = params
    child.stdin.write(json.dumps(request) + "\n")
    child.stdin.flush()
    # ``session.start`` emits 12 ``$/progress`` notifications before the
    # final result; skip them so callers see the response envelope.
    while True:
        line = child.stdout.readline()
        if not line:
            return {}
        payload = json.loads(line)
        if payload.get("id") == req_id:
            return payload  # type: ignore[no-any-return]


def test_active_sidecar_rss_under_warning_band(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Sidecar + running EmbeddedBridge fits the 3× warning band of §8.

    Active memory is sidecar process + tracked agent subprocesses. We
    don't spawn agents here (that requires a real RL engine boot); the
    floor measurement is sidecar + uvicorn-hosted dashboard bridge,
    which is what the bridge phase of session.start brings up. As
    orchestrator integration lands and agents are spawned, this test
    will widen to include their RSS.
    """
    # Project skeleton the session.start preparation will accept:
    # agentshore.yaml present + .beads/ directory present.
    project_path = tmp_path / "nfr-project"
    project_path.mkdir()
    (project_path / "agentshore.yaml").write_text(VALID_TIERED_CONFIG, encoding="utf-8")
    (project_path / ".beads").mkdir()

    child = _spawn_sidecar()
    try:
        _send_handshake(child)
        select_response = _send_rpc(child, 2, "project.select", {"path": str(project_path)})
        assert "result" in select_response, select_response
        start_response = _send_rpc(child, 3, "session.start")
        assert "result" in start_response, start_response
        # Allow uvicorn worker threads to reach steady-state.
        time.sleep(0.5)
        rss_kb = _read_rss_kb(child.pid)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)

    assert rss_kb > 0, "ps returned no RSS — process gone before measurement?"
    rss_mb = rss_kb / 1024.0

    with capsys.disabled():
        target_marker = "" if rss_mb <= ACTIVE_RSS_TARGET_MB else " [over target]"
        print(
            f"\n[NFR §8] active sidecar RSS (sidecar + bridge, no agents): "
            f"{rss_mb:.1f} MB (target {ACTIVE_RSS_TARGET_MB:.0f}, "
            f"warn {ACTIVE_RSS_WARN_MB:.0f}){target_marker}"
        )

    assert rss_mb < ACTIVE_RSS_WARN_MB, (
        f"active sidecar RSS {rss_mb:.1f}MB exceeded warning band {ACTIVE_RSS_WARN_MB:.0f}MB"
    )


def test_nfr_module_imports_path_is_documented() -> None:
    """Smoke test: the NFR module file is the canonical doc pointer.

    Keeps the docstring at the top of the file findable via grep so a
    future maintainer adjusting the bands sees the design reference.
    """
    here = Path(__file__).resolve()
    text = here.read_text(encoding="utf-8")
    assert "DESIGN §8" in text, "NFR test must cite DESIGN §8"
    assert "desktop-n7s" in text, "NFR test must cite the bead"
