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


def _prune_local_checkpoints(weights_dir: Path, keep: int = _LOCAL_CHECKPOINT_KEEP) -> None:
    """Delete numbered local checkpoints beyond the most recent `keep` files."""
    numbered = sorted(weights_dir.glob("policy_[0-9][0-9][0-9][0-9][0-9][0-9].pt"))
    for stale in numbered[:-keep]:
        with contextlib.suppress(OSError):
            stale.unlink()


_FLOW_BLOCK_MIGRATABLE_OBSERVATION_VERSIONS: frozenset[int] = frozenset({16})
_FIRST_LAYER_WEIGHT_KEY = "trunk.0.weight"


def migrate_flow_block_checkpoint(source: Path, dest: Path) -> bool:
    """Carry a pre-v17 checkpoint forward to the current observation layout.

    v17 reuses slots 2-7, which every earlier compatible version fed as constant
    zeros. Zeroing the first layer's weights for those columns and re-stamping the
    version gives a policy that produces the same outputs it did before for any
    state (the new inputs are multiplied by zero), while leaving those weights
    free to learn the new signals. Trained behaviour is preserved; nothing is
    retrained.

    Returns True when *dest* was written. Any other version, shape, or head
    mismatch returns False and writes nothing — the caller then falls back to the
    normal seed / cold-start path.
    """
    from agentshore.rl.observation import FLOW_BLOCK_END, FLOW_BLOCK_START, OBSERVATION_DIM

    try:
        payload = torch.load(source, map_location="cpu", weights_only=True)
    except (OSError, RuntimeError, ValueError) as exc:
        _logger.warning("flow_block_migration_load_failed", path=str(source), error=str(exc))
        return False
    if not isinstance(payload, dict):
        return False
    state_dict = payload.get("state_dict")
    if not isinstance(state_dict, dict):
        return False
    weight = state_dict.get(_FIRST_LAYER_WEIGHT_KEY)
    if (
        payload.get("observation_version") not in _FLOW_BLOCK_MIGRATABLE_OBSERVATION_VERSIONS
        or payload.get("action_space_version") != ACTION_SPACE_VERSION
        or payload.get("policy_version") != POLICY_VERSION
        or payload.get("obs_dim") != OBSERVATION_DIM
        or not isinstance(weight, torch.Tensor)
        or weight.ndim != 2
        or weight.shape[1] != OBSERVATION_DIM
    ):
        return False

    migrated_weight = weight.clone()
    migrated_weight[:, FLOW_BLOCK_START:FLOW_BLOCK_END] = 0.0
    migrated = dict(payload)
    migrated["state_dict"] = {**state_dict, _FIRST_LAYER_WEIGHT_KEY: migrated_weight}
    migrated["observation_version"] = OBSERVATION_VERSION

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        os.close(fd)
        torch.save(migrated, tmp)
        os.replace(tmp, dest)
    except (OSError, RuntimeError) as exc:
        _logger.warning("flow_block_migration_write_failed", path=str(dest), error=str(exc))
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False
    _logger.info(
        "flow_block_checkpoint_migrated",
        source=str(source),
        dest=str(dest),
        from_observation_version=payload.get("observation_version"),
        to_observation_version=OBSERVATION_VERSION,
    )
    return True


def migrate_previous_observation_canonical(weights_dir: Path) -> bool:
    """Create the current canonical from a migratable previous-version one.

    No-op when the current canonical already exists (never overwrite live
    weights) or no migratable predecessor is present. The predecessor is left in
    place for ``_archive_old_canonicals`` to quarantine as usual.
    """
    current = weights_dir / canonical_weights_filename()
    if current.exists():
        return False
    for version in sorted(_FLOW_BLOCK_MIGRATABLE_OBSERVATION_VERSIONS, reverse=True):
        previous = weights_dir / f"policy_a{ACTION_SPACE_VERSION}_o{version}_p{POLICY_VERSION}.pt"
        if previous.is_file() and migrate_flow_block_checkpoint(previous, current):
            return True
    return False


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
    migrate_previous_observation_canonical(weights_dir)
    current = weights_dir / canonical_weights_filename()
    for f in sorted(weights_dir.glob("policy_*.pt")):
        if f == current or f.name.startswith("policy_legacy_"):
            continue
        stem = f.stem
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
        try:
            delta: dict[str, torch.Tensor] | None = {
                k: current_sd[k] - reload_base[k] for k in reload_base
            }
        except (KeyError, RuntimeError):
            delta = None
    else:
        delta = None

    with _exclusive_file_lock(lock_path):
        if delta is not None and canonical.exists():
            try:
                base = ActorCritic.load(canonical)
                if base.num_configs == policy.num_configs:
                    merged_sd = {k: base.state_dict()[k] + delta[k] for k in delta}
                    base.load_state_dict(merged_sd)
                    to_save = base
                else:
                    to_save = policy
            except (IncompatibleCheckpointError, KeyError, RuntimeError):
                to_save = policy
        else:
            to_save = policy

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
