"""Tests for rl/selector.py weight lifecycle helpers.

Covers:
- _prune_local_checkpoints: keeps only the last N numbered checkpoint files
- _archive_old_canonicals: renames policy_v{N}.pt for N != POLICY_VERSION
- delta accumulation: _write_global_canonical merges concurrent updates
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import torch

from agentshore.rl.action_space import POLICY_VERSION
from agentshore.rl.policy import ActorCritic
from agentshore.rl.selector import _archive_old_canonicals, _prune_local_checkpoints

# ---------------------------------------------------------------------------
# _prune_local_checkpoints
# ---------------------------------------------------------------------------


def test_prune_keeps_last_n(tmp_path: Path) -> None:
    """With 5 numbered checkpoints and keep=2, only the last 2 remain."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()

    # Create 5 fake numbered checkpoints in sorted order.
    names = [
        "policy_000001.pt",
        "policy_000002.pt",
        "policy_000003.pt",
        "policy_000004.pt",
        "policy_000005.pt",
    ]
    for name in names:
        (weights_dir / name).write_bytes(b"fake")

    _prune_local_checkpoints(weights_dir, keep=2)

    remaining = sorted(weights_dir.glob("policy_[0-9][0-9][0-9][0-9][0-9][0-9].pt"))
    assert len(remaining) == 2
    assert remaining[0].name == "policy_000004.pt"
    assert remaining[1].name == "policy_000005.pt"


def test_prune_noop_when_fewer_than_keep(tmp_path: Path) -> None:
    """When there are fewer files than keep, nothing is deleted."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()

    (weights_dir / "policy_000001.pt").write_bytes(b"fake")

    _prune_local_checkpoints(weights_dir, keep=2)

    remaining = list(weights_dir.glob("policy_[0-9][0-9][0-9][0-9][0-9][0-9].pt"))
    assert len(remaining) == 1


def test_prune_empty_dir_is_noop(tmp_path: Path) -> None:
    """Empty directory does not raise."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()
    _prune_local_checkpoints(weights_dir, keep=2)
    assert list(weights_dir.iterdir()) == []


def test_prune_does_not_touch_non_numbered_files(tmp_path: Path) -> None:
    """Files that don't match the numbered pattern are left untouched."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()

    for i in range(5):
        (weights_dir / f"policy_{i:06d}.pt").write_bytes(b"fake")

    # These should never be pruned by _prune_local_checkpoints.
    canonical = weights_dir / f"policy_v{POLICY_VERSION}.pt"
    canonical.write_bytes(b"canonical")
    legacy = weights_dir / "policy_legacy_v1.pt"
    legacy.write_bytes(b"legacy")

    _prune_local_checkpoints(weights_dir, keep=2)

    assert canonical.exists(), "canonical file was incorrectly deleted"
    assert legacy.exists(), "legacy file was incorrectly deleted"


# ---------------------------------------------------------------------------
# _archive_old_canonicals
# ---------------------------------------------------------------------------


def test_archive_renames_old_version(tmp_path: Path) -> None:
    """policy_v{OLD}.pt is renamed to policy_legacy_v{OLD}.pt."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()

    old_version = POLICY_VERSION - 1
    if old_version < 0:
        pytest.skip("POLICY_VERSION is 0, cannot create an older version")

    old_file = weights_dir / f"policy_v{old_version}.pt"
    old_file.write_bytes(b"old weights")

    _archive_old_canonicals(weights_dir)

    assert not old_file.exists(), "old canonical was not renamed"
    expected_dest = weights_dir / f"policy_legacy_v{old_version}.pt"
    assert expected_dest.exists(), "legacy-named file not found after archive"


def test_archive_leaves_current_version_untouched(tmp_path: Path) -> None:
    """policy_v{POLICY_VERSION}.pt is never renamed."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()

    current_file = weights_dir / f"policy_v{POLICY_VERSION}.pt"
    current_file.write_bytes(b"current weights")

    _archive_old_canonicals(weights_dir)

    assert current_file.exists(), "current version canonical was incorrectly renamed"
    assert not list(weights_dir.glob("policy_legacy_*.pt")), "unexpected legacy file created"


def test_archive_handles_multiple_old_versions(tmp_path: Path) -> None:
    """Multiple stale canonicals are all archived in one call."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()

    stale_versions = [v for v in range(max(0, POLICY_VERSION - 3), POLICY_VERSION)]
    if not stale_versions:
        pytest.skip("POLICY_VERSION too small to test multiple stale versions")

    for v in stale_versions:
        (weights_dir / f"policy_v{v}.pt").write_bytes(b"stale")

    # Also create the current version so it is definitely left alone.
    current_file = weights_dir / f"policy_v{POLICY_VERSION}.pt"
    current_file.write_bytes(b"current")

    _archive_old_canonicals(weights_dir)

    # All stale versions should be gone.
    for v in stale_versions:
        assert not (weights_dir / f"policy_v{v}.pt").exists(), (
            f"stale policy_v{v}.pt was not archived"
        )
    # Current version untouched.
    assert current_file.exists()
    # Legacy files exist for each stale version.
    legacy_files = list(weights_dir.glob("policy_legacy_*.pt"))
    assert len(legacy_files) == len(stale_versions)


def test_archive_skips_already_legacy_named(tmp_path: Path) -> None:
    """Files already named policy_legacy_v*.pt are not re-processed."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()

    legacy_existing = weights_dir / "policy_legacy_v0.pt"
    legacy_existing.write_bytes(b"already archived")

    _archive_old_canonicals(weights_dir)

    # File should still exist and not be double-archived.
    assert legacy_existing.exists()


def test_archive_empty_dir_is_noop(tmp_path: Path) -> None:
    """Empty directory does not raise."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()
    _archive_old_canonicals(weights_dir)
    assert list(weights_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# _write_global_canonical — delta accumulation
# ---------------------------------------------------------------------------


def _make_selector_stub(weights_dir: Path) -> MagicMock:
    """Return a minimal PPOSelector-like stub with a real ActorCritic policy."""
    from agentshore.rl.selector import PPOSelector

    stub = MagicMock(spec=PPOSelector)
    policy = ActorCritic()
    stub._policy = policy
    stub._reload_base = None
    stub._write_global_canonical_blocking = PPOSelector._write_global_canonical_blocking.__get__(
        stub
    )
    return stub


def _policy_with_sd(sd: dict[str, torch.Tensor]) -> ActorCritic:
    """Return an ActorCritic whose state dict is set to *sd*."""
    p = ActorCritic()
    p.load_state_dict(sd)
    return p


def _add_scalar_to_sd(sd: dict[str, torch.Tensor], scalar: float) -> dict[str, torch.Tensor]:
    return {k: v + scalar for k, v in sd.items()}


def test_delta_full_write_when_no_base(tmp_path: Path) -> None:
    """Without a reload base, _write_global_canonical does a full write."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()
    canonical = weights_dir / f"policy_v{POLICY_VERSION}.pt"
    lock = weights_dir / f"policy_v{POLICY_VERSION}.lock"

    stub = _make_selector_stub(weights_dir)
    stub._reload_base = None
    original_sd = {k: v.clone() for k, v in stub._policy.state_dict().items()}

    stub._write_global_canonical_blocking(canonical, lock)

    assert canonical.exists()
    loaded = ActorCritic.load(canonical)
    for k in original_sd:
        assert torch.allclose(loaded.state_dict()[k], original_sd[k])


def test_delta_accumulates_onto_existing_global(tmp_path: Path) -> None:
    """Delta from one session is added to an existing global canonical."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()
    canonical = weights_dir / f"policy_v{POLICY_VERSION}.pt"
    lock = weights_dir / f"policy_v{POLICY_VERSION}.lock"

    # The global canonical represents prior learning.
    global_sd = {k: v.clone() for k, v in ActorCritic().state_dict().items()}
    _policy_with_sd(global_sd).save(canonical)

    # Session: reloaded global as base, PPO shifted all weights by +0.1.
    after_update_sd = _add_scalar_to_sd(global_sd, 0.1)
    stub = _make_selector_stub(weights_dir)
    stub._reload_base = {k: v.clone() for k, v in global_sd.items()}
    stub._policy = _policy_with_sd(after_update_sd)

    stub._write_global_canonical_blocking(canonical, lock)

    merged = ActorCritic.load(canonical)
    for k in global_sd:
        expected = global_sd[k] + (after_update_sd[k] - global_sd[k])  # = global + 0.1
        assert torch.allclose(merged.state_dict()[k], expected, atol=1e-5), (
            f"delta not applied correctly for {k}"
        )


def test_delta_concurrent_sessions_both_preserved(tmp_path: Path) -> None:
    """Two sessions writing deltas sequentially both have their updates preserved."""
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()
    canonical = weights_dir / f"policy_v{POLICY_VERSION}.pt"
    lock = weights_dir / f"policy_v{POLICY_VERSION}.lock"

    # Shared starting point.
    base_sd = {k: v.clone() for k, v in ActorCritic().state_dict().items()}
    _policy_with_sd(base_sd).save(canonical)

    # Session A: delta +0.1 on all parameters.
    stub_a = _make_selector_stub(weights_dir)
    stub_a._reload_base = {k: v.clone() for k, v in base_sd.items()}
    stub_a._policy = _policy_with_sd(_add_scalar_to_sd(base_sd, 0.1))

    # Session B: delta +0.2 on all parameters.
    stub_b = _make_selector_stub(weights_dir)
    stub_b._reload_base = {k: v.clone() for k, v in base_sd.items()}
    stub_b._policy = _policy_with_sd(_add_scalar_to_sd(base_sd, 0.2))

    # A writes, then B reads current global (now base+0.1) and adds its delta.
    stub_a._write_global_canonical_blocking(canonical, lock)
    stub_b._write_global_canonical_blocking(canonical, lock)

    # Final = base + 0.1 (from A) + 0.2 (B's delta applied on top) = base + 0.3.
    final = ActorCritic.load(canonical)
    for k, base_v in base_sd.items():
        expected = base_v + 0.3
        assert torch.allclose(final.state_dict()[k], expected, atol=1e-5), (
            f"concurrent deltas not both preserved for {k}"
        )
