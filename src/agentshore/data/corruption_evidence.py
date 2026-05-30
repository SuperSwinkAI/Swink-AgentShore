"""Best-effort evidence collection when SQLite corruption is detected.

When ``data.integrity._run_canary`` or ``restore_from_snapshot_ring`` sees
``quick_check`` fail, this module captures surrounding system state so a
post-mortem can attribute the corruption to its root cause (suspected
macOS screen-lock / I/O throttling per the ``desktop-tvsb`` lineage).

The collector is structured as a single ``capture_corruption_evidence``
helper that gathers DB file stats, power state, system-log tail, fd
holders, caffeinate status, and write-recency into one dict. Every
sub-step is best-effort: failures are recorded in the dict (under a
``_errors`` key) but never raise — corruption evidence is diagnostic, not
load-bearing.

Output gets emitted as a single ``db_corruption_evidence_captured``
structured log event in the caller. The ``corruption_event_id`` UUID lets
future post-mortems correlate the evidence with the originating canary
or restore log line.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger(__name__)

# Truncation limits — generous but bounded so evidence stays loggable.
_LOG_OUTPUT_BYTES_LIMIT = 8 * 1024
_LSOF_OUTPUT_BYTES_LIMIT = 4 * 1024
_PMSET_OUTPUT_BYTES_LIMIT = 2 * 1024


def _truncate(text: str, limit: int) -> str:
    """Return ``text`` truncated to ``limit`` bytes, with a marker."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="replace") + "\n... [truncated]"


def _stat_file(path: Path) -> dict[str, Any] | None:
    """Stat ``path`` and return size + mtime + mode; ``None`` if missing."""
    try:
        st = path.stat()
    except OSError:
        return None
    return {
        "size_bytes": int(st.st_size),
        "mtime_unix": float(st.st_mtime),
        "mode_octal": oct(st.st_mode & 0o777),
    }


def _run_capture(
    args: list[str],
    *,
    timeout: float = 3.0,
    byte_limit: int = _LOG_OUTPUT_BYTES_LIMIT,
) -> tuple[str | None, str | None]:
    """Run ``args``; return ``(stdout_truncated, error_message)``.

    On success: ``(stdout, None)``. On any failure (missing binary,
    timeout, nonzero exit, OS error): ``(None, "<reason>")``. Stdout is
    truncated to ``byte_limit`` bytes.
    """
    if shutil.which(args[0]) is None:
        return None, f"{args[0]} not on PATH"
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            args,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"{args[0]} not found"
    except subprocess.TimeoutExpired:
        return None, f"{args[0]} timed out after {timeout}s"
    except OSError as exc:
        return None, f"{args[0]} OSError: {exc}"
    if result.returncode != 0 and not result.stdout:
        return None, (
            f"{args[0]} rc={result.returncode} "
            f"stderr={result.stderr.decode('utf-8', errors='replace')[:200]}"
        )
    return _truncate(result.stdout.decode("utf-8", errors="replace"), byte_limit), None


def _power_state() -> dict[str, Any]:
    """Capture power / battery / sleep state. macOS uses ``pmset``."""
    if sys.platform != "darwin":
        return {"_skipped": f"power capture only implemented for macOS (got {sys.platform})"}
    out: dict[str, Any] = {}
    ps_stdout, ps_err = _run_capture(
        ["pmset", "-g", "ps"], byte_limit=_PMSET_OUTPUT_BYTES_LIMIT
    )
    out["pmset_ps"] = ps_stdout
    if ps_err:
        out["pmset_ps_error"] = ps_err
    batt_stdout, batt_err = _run_capture(
        ["pmset", "-g", "batt"], byte_limit=_PMSET_OUTPUT_BYTES_LIMIT
    )
    out["pmset_batt"] = batt_stdout
    if batt_err:
        out["pmset_batt_error"] = batt_err
    return out


def _system_log_tail() -> dict[str, Any]:
    """Capture recent kernel/sleep/wake log lines.

    Window kept tight (1m) and timeout aggressive (3s) because the macOS
    ``log show`` daemon is famously slow — a 5-minute window with predicate
    can take 15-30s, which would freeze real corruption-recovery bootstrap.
    Partial / timed-out captures are still useful: the surrounding pmset
    and file-stat evidence is what attribution usually hinges on, and a
    ``log_show_error`` value of ``"timed out"`` is itself a signal that the
    machine was under load.
    """
    if sys.platform == "darwin":
        stdout, err = _run_capture(
            [
                "log",
                "show",
                "--last",
                "1m",
                "--predicate",
                'subsystem == "com.apple.kernel"',
                "--info",
                "--style",
                "compact",
            ],
            timeout=3.0,
        )
        return {"log_show": stdout, "log_show_error": err}
    if sys.platform.startswith("linux"):
        stdout, err = _run_capture(
            ["journalctl", "-n", "50", "--since", "1 minute ago", "--no-pager"],
            timeout=3.0,
        )
        return {"journalctl": stdout, "journalctl_error": err}
    return {"_skipped": f"system log capture unsupported on {sys.platform}"}


def _fd_holders(db_path: Path) -> dict[str, Any]:
    """Capture ``lsof`` output for the DB file."""
    stdout, err = _run_capture(
        ["lsof", str(db_path)], timeout=3.0, byte_limit=_LSOF_OUTPUT_BYTES_LIMIT
    )
    out: dict[str, Any] = {"lsof": stdout}
    if err:
        out["lsof_error"] = err
    return out


def _caffeinate_status() -> dict[str, Any]:
    """True when at least one ``caffeinate`` process is running on this host.

    Best-effort — checks via ``pgrep -f caffeinate``. macOS only by design.
    """
    if sys.platform != "darwin":
        return {"_skipped": "caffeinate is macOS-only"}
    if shutil.which("pgrep") is None:
        return {"_error": "pgrep not on PATH"}
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["pgrep", "-f", "caffeinate"],
            capture_output=True,
            check=False,
            timeout=3.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {"_error": str(exc)}
    pids = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
    return {"running": result.returncode == 0 and bool(pids), "pids": pids[:10]}


def capture_corruption_evidence(db_path: Path) -> dict[str, Any]:
    """Gather diagnostic state surrounding a SQLite corruption detection.

    Returns a dict with the captured signals. Always includes
    ``corruption_event_id`` (UUID) and ``captured_at_unix``. Sub-step
    failures are recorded under nested ``*_error`` keys but never raise.

    Suitable for emitting as the ``data`` payload of a single structured
    log event so post-mortems can correlate downstream symptoms back to
    the moment corruption was detected.
    """
    evidence: dict[str, Any] = {
        "corruption_event_id": str(uuid.uuid4()),
        "captured_at_unix": time.time(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "db_path": str(db_path),
    }

    # DB file family (main + WAL + SHM)
    files: dict[str, Any] = {}
    for suffix in ("", "-wal", "-shm"):
        sibling = db_path.with_name(db_path.name + suffix) if suffix else db_path
        files[sibling.name] = _stat_file(sibling)
    evidence["db_files"] = files

    # Time since last successful write (approximated via main file mtime)
    main_stat = files.get(db_path.name)
    if isinstance(main_stat, dict):
        evidence["seconds_since_db_mtime"] = max(
            0.0, time.time() - float(main_stat["mtime_unix"])
        )

    # Power + caffeinate
    evidence["power_state"] = _power_state()
    evidence["caffeinate"] = _caffeinate_status()

    # System log + fd holders
    evidence["system_log"] = _system_log_tail()
    evidence["fd_holders"] = _fd_holders(db_path)

    return evidence


__all__ = ["capture_corruption_evidence"]
