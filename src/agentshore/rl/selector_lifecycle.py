"""Checkpoint reload/save orchestration for ``PPOSelector``.

Split out of ``rl/selector.py`` (TNQA wave-2 line-count reduction). Houses the
two operations that touch selector-owned policy/reload state — reloading the
shared global canonical before a PPO update, and writing a local + global
checkpoint after one — as functions parameterized on the pieces of that state
they need, so the ``PPOSelector`` methods stay thin call-throughs. The
low-level, stateless file I/O these call into (locking, the delta-merge
write, local-checkpoint pruning) stays in ``checkpoint_store.py``; this module
is the stateful orchestration layer on top of it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from agentshore.rl.checkpoint_store import (
    _prune_local_checkpoints,
    canonical_lock_filename,
    canonical_weights_filename,
    write_global_canonical_blocking,
)

if TYPE_CHECKING:
    import torch

    from agentshore.data.store import DataStore
    from agentshore.rl.policy import ActorCritic

_logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReloadOutcome:
    """Result of one shared-weights reload attempt.

    ``reload_base`` is ``None`` both when nothing changed (no shared canonical
    on disk yet) and when the reload was rejected — only a successful reload
    produces a new base. The caller must NOT clear its existing
    ``_reload_base`` on a ``None`` result here, only replace it when one is
    given (mirrors the pre-split ``_reload_shared_weights`` contract).
    """

    rejected: bool
    reload_base: dict[str, torch.Tensor] | None


def reload_shared_weights(policy: ActorCritic, global_weights_dir: Path) -> ReloadOutcome:
    """Load the latest shared policy from disk into *policy*, with version safety.

    Mutates *policy* in place via ``load_state_dict`` on success. A rejected
    reload (the on-disk canonical exists but is incompatible with this
    session — load failure or config-index mismatch) is reported via
    ``ReloadOutcome.rejected=True`` so the caller's ``save_checkpoint`` skips
    the global canonical write. A missing canonical is NOT a rejection — the
    first-write / full-overwrite path stays valid.
    """
    from agentshore.rl.policy import ActorCritic, IncompatibleCheckpointError

    shared = global_weights_dir / canonical_weights_filename()
    if not shared.exists():
        return ReloadOutcome(rejected=False, reload_base=None)
    try:
        new_policy = ActorCritic.load(shared)
    except IncompatibleCheckpointError as exc:
        _logger.warning("ppo_selector.checkpoint_incompatible", path=str(shared), error=str(exc))
        return ReloadOutcome(rejected=True, reload_base=None)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _logger.warning("ppo_selector.reload_failed", path=str(shared), error=str(exc))
        return ReloadOutcome(rejected=True, reload_base=None)
    if new_policy.num_configs != policy.num_configs:
        _logger.warning(
            "ppo_selector.config_index_changed",
            saved=new_policy.num_configs,
            current=policy.num_configs,
        )
        return ReloadOutcome(rejected=True, reload_base=None)
    new_sd = new_policy.state_dict()
    reload_base = {k: v.clone() for k, v in new_sd.items()}
    policy.load_state_dict(new_sd)
    _logger.debug("ppo_selector.weights_reloaded", path=str(shared))
    return ReloadOutcome(rejected=False, reload_base=reload_base)


async def save_checkpoint(
    *,
    policy: ActorCritic,
    reload_base: dict[str, torch.Tensor] | None,
    reload_rejected: bool,
    global_weights_dir: Path,
    store: DataStore,
    session_id: str,
    weights_dir: Path,
    total_plays: int,
) -> None:
    """Write a numbered local checkpoint and update the global canonical.

    Local numbered checkpoints provide crash recovery; only the last
    ``_LOCAL_CHECKPOINT_KEEP`` are kept. The version-tagged canonical
    (``canonical_weights_filename()``) is written to the global
    ``~/.config/swink/agentshore/weights/`` directory so all projects
    contribute to and benefit from a shared policy.

    Delta accumulation: each session computes delta = (post-update weights) -
    (pre-update base), then under an exclusive file lock reads the current
    global, adds the delta, and writes back atomically — so concurrent
    sessions from different projects don't overwrite each other's learning.
    If no reload base exists (first update of this session, or reload was
    skipped), ``write_global_canonical_blocking`` falls back to a full
    overwrite — equivalent to the old behaviour and still correct since delta
    == full weights when base == zero.

    Contract (see ``reload_shared_weights``): when the latest reload was
    REJECTED (the on-disk canonical exists but is incompatible with this
    session), the global write is skipped entirely. This session is training
    on stale local weights relative to that canonical, so contributing a
    delta against a mismatched base would corrupt the shared checkpoint. The
    local numbered checkpoint (above) still persists for crash recovery.
    """
    from agentshore.data.store import CheckpointRecord

    weights_dir = Path(weights_dir)
    weights_dir.mkdir(parents=True, exist_ok=True)

    # Numbered local checkpoint for crash recovery.
    weights_path = weights_dir / f"policy_{total_plays:06d}.pt"
    policy.save(weights_path)

    global_weights_dir.mkdir(parents=True, exist_ok=True)
    global_canonical = global_weights_dir / canonical_weights_filename()
    lock_path = global_weights_dir / canonical_lock_filename()
    if reload_rejected:
        _logger.warning(
            "ppo_selector.global_canonical_write_skipped",
            path=str(global_canonical),
            reason="reload_rejected_incompatible_canonical",
        )
    else:
        try:
            await asyncio.to_thread(
                write_global_canonical_blocking,
                policy,
                reload_base,
                global_canonical,
                lock_path,
            )
        except OSError as exc:
            _logger.warning(
                "ppo_selector.global_canonical_update_failed",
                path=str(global_canonical),
                error=str(exc),
            )

    # Keep local dir lean — crash recovery only needs the last few.
    _prune_local_checkpoints(weights_dir)

    record = CheckpointRecord(
        session_id=session_id,
        created_at=datetime.now(UTC).isoformat(),
        play_count=total_plays,
        weights_path=str(weights_path),
    )
    await store.save_checkpoint(record)
    _logger.info(
        "ppo_selector.checkpoint_saved",
        path=str(weights_path),
        global_canonical=str(global_canonical),
        total_plays=total_plays,
        delta_merge=reload_base is not None,
        global_write_skipped=reload_rejected,
    )
