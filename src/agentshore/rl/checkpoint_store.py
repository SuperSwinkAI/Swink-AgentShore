"""Checkpoint file I/O for the PPO selector.

Houses the on-disk lifecycle that has nothing to do with play selection: the
cross-platform advisory file locks, local-checkpoint pruning, legacy-canonical
archival, and the delta-merge write of the global canonical. ``selector.py``
imports these and delegates to them so its ``select()`` orchestration stays
focused on selection.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import structlog
import torch

from agentshore.rl.action_space import ACTION_SPACE_VERSION
from agentshore.rl.config_head import POLICY_VERSION
from agentshore.rl.observation import OBSERVATION_VERSION

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO

    from agentshore.rl.policy import ActorCritic

_logger = structlog.get_logger(__name__)

_LOCAL_CHECKPOINT_KEEP = 2


def canonical_weights_filename() -> str:
    """Filename for the global canonical weights, tagged with all three versions.

    The three version ints that govern checkpoint compatibility are independent:
    ``ACTION_SPACE_VERSION`` (play head shape), ``OBSERVATION_VERSION`` (state
    vector layout), and ``POLICY_VERSION`` (config head). Encoding all three in
    the filename makes it unambiguous which checkpoint matches the running code —
    no single version int silently "owns" the file.
    """
    return f"policy_a{ACTION_SPACE_VERSION}_o{OBSERVATION_VERSION}_p{POLICY_VERSION}.pt"


def canonical_lock_filename() -> str:
    """Lock-file name paired with :func:`canonical_weights_filename`."""
    return f"policy_a{ACTION_SPACE_VERSION}_o{OBSERVATION_VERSION}_p{POLICY_VERSION}.lock"


class _WindowsLockingModule(Protocol):
    LK_LOCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int, /) -> int: ...


# Local / canonical checkpoint lifecycle


def _prune_local_checkpoints(weights_dir: Path, keep: int = _LOCAL_CHECKPOINT_KEEP) -> None:
    """Delete numbered local checkpoints beyond the most recent `keep` files."""
    numbered = sorted(weights_dir.glob("policy_[0-9][0-9][0-9][0-9][0-9][0-9].pt"))
    for stale in numbered[:-keep]:
        with contextlib.suppress(OSError):
            stale.unlink()


def _archive_old_canonicals(weights_dir: Path) -> None:
    """Quarantine canonical weights that don't match the current version triple.

    The current canonical is ``canonical_weights_filename()`` (tagged with the
    live action/observation/policy versions). Any other canonical — including the
    old single-version ``policy_v{N}.pt`` scheme and any stale triple from a prior
    code revision — is renamed to a ``policy_legacy_<stem>.pt`` form so the user
    can inspect it. A fresh project with no matching file simply cold-starts.

    Sibling to cleanup_stale_canonical_weights (which handles the legacy unnamed
    policy.pt). Never deletes — renames so the user can inspect.
    """
    current = weights_dir / canonical_weights_filename()
    for f in sorted(weights_dir.glob("policy_*.pt")):
        if f == current or f.name.startswith("policy_legacy_"):
            continue
        stem = f.stem  # e.g. "policy_v2" or "policy_a13_o12_p5"
        # Skip numbered local checkpoints — managed by _prune_local_checkpoints, not here.
        suffix = stem[len("policy_") :]
        if suffix.isdigit():
            continue
        dest = weights_dir / f"policy_legacy_{suffix}.pt"
        with contextlib.suppress(OSError):
            f.rename(dest)


def cleanup_stale_canonical_weights(weights_dir: Path) -> None:
    """Rename policy.pt to policy_legacy_v{N}.pt if it's version-incompatible.

    Called at session start. Never deletes — just renames so the user can inspect.
    """
    from agentshore.rl.policy import ActorCritic

    legacy = weights_dir / "policy.pt"
    if not legacy.exists():
        return
    try:
        from agentshore.rl.policy import IncompatibleCheckpointError

        ActorCritic.load(legacy)
        # Compatible — leave it alone.
    except IncompatibleCheckpointError:
        try:
            payload = torch.load(legacy, map_location="cpu", weights_only=True)
            saved_ver = (
                payload.get("policy_version", "unknown") if isinstance(payload, dict) else "unknown"
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            _logger.warning("legacy_checkpoint_load_failed", path=str(legacy), error=str(exc))
            saved_ver = "unknown"
        dest = weights_dir / f"policy_legacy_v{saved_ver}.pt"
        legacy.rename(dest)
        _logger.warning(
            "stale_canonical_checkpoint_renamed",
            from_path=str(legacy),
            to_path=str(dest),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _logger.warning("cleanup_stale_canonical_failed", error=str(exc))


# Cross-platform advisory file locks


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for ``path`` on POSIX and Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        _lock_file(lock_file)
        try:
            yield
        finally:
            _unlock_file(lock_file)


def _lock_file(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        _prepare_windows_lock_byte(lock_file)
        win_lock = cast("_WindowsLockingModule", msvcrt)
        win_lock.locking(lock_file.fileno(), win_lock.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file, fcntl.LOCK_EX)


def _unlock_file(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        win_lock = cast("_WindowsLockingModule", msvcrt)
        win_lock.locking(lock_file.fileno(), win_lock.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file, fcntl.LOCK_UN)


def _prepare_windows_lock_byte(lock_file: BinaryIO) -> None:
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)


# Global canonical delta-merge write


def write_global_canonical_blocking(
    policy: ActorCritic,
    reload_base: dict[str, torch.Tensor] | None,
    canonical: Path,
    lock_path: Path,
) -> None:
    """Apply this session's gradient delta to the global canonical under a lock.

    Three sessions writing simultaneously each read the current global,
    add their own delta, and write back.  The exclusive flock serialises the
    read-modify-write so no session's update is lost.

    This function performs only synchronous I/O and must be called via
    ``asyncio.to_thread`` from the async ``save_checkpoint`` path.
    """
    from agentshore.rl.policy import ActorCritic, IncompatibleCheckpointError

    current_sd = policy.state_dict()

    if reload_base is not None:
        # Compute what this PPO update actually changed.
        try:
            delta: dict[str, torch.Tensor] | None = {
                k: current_sd[k] - reload_base[k] for k in reload_base
            }
        except (KeyError, RuntimeError):
            # Shape mismatch — architecture changed mid-session; full write.
            delta = None
    else:
        delta = None  # No base snapshot; write full weights.

    with _exclusive_file_lock(lock_path):
        if delta is not None and canonical.exists():
            try:
                base = ActorCritic.load(canonical)
                if base.num_configs == policy.num_configs:
                    merged_sd = {k: base.state_dict()[k] + delta[k] for k in delta}
                    base.load_state_dict(merged_sd)
                    to_save = base
                else:
                    to_save = policy  # config index mismatch; full write
            except (IncompatibleCheckpointError, KeyError, RuntimeError):
                to_save = policy  # incompatible global; full write
        else:
            to_save = policy  # no delta or no existing global; full write

        canonical.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=canonical.parent, suffix=".pt.tmp")
        try:
            os.close(tmp_fd)
            to_save.save(Path(tmp_path))
            os.replace(tmp_path, canonical)
        except (OSError, RuntimeError):
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise

    _logger.debug(
        "ppo_selector.global_canonical_updated",
        path=str(canonical),
        mode="delta" if (delta is not None and canonical.exists()) else "full",
    )
