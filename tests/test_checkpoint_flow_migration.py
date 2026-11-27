"""v16 → v17 checkpoint migration: reuse of the zero-filled observation slots 2-7."""

from __future__ import annotations

from pathlib import Path

import torch

from agentshore.rl.action_space import ACTION_SPACE_VERSION
from agentshore.rl.checkpoint_store import (
    _archive_old_canonicals,
    canonical_weights_filename,
    migrate_flow_block_checkpoint,
    migrate_previous_observation_canonical,
)
from agentshore.rl.config_head import POLICY_VERSION
from agentshore.rl.observation import (
    FLOW_BLOCK_END,
    FLOW_BLOCK_START,
    OBSERVATION_DIM,
    OBSERVATION_VERSION,
)
from agentshore.rl.policy import ActorCritic

_V16_NAME = f"policy_a{ACTION_SPACE_VERSION}_o16_p{POLICY_VERSION}.pt"


def _write_v16(path: Path, *, num_configs: int = 3) -> ActorCritic:
    """Write a checkpoint stamped as observation v16 and return the model in it."""
    model = ActorCritic(num_configs=num_configs)
    model.save(path)
    payload = torch.load(path, map_location="cpu", weights_only=True)
    payload["observation_version"] = 16
    torch.save(payload, path)
    model.eval()
    return model


def test_migrated_policy_matches_the_original_on_pre_v17_states(tmp_path: Path) -> None:
    source, dest = tmp_path / "old.pt", tmp_path / "new.pt"
    original = _write_v16(source)

    assert migrate_flow_block_checkpoint(source, dest) is True
    migrated = ActorCritic.load(dest)

    assert migrated.num_configs == original.num_configs
    states = torch.rand(32, OBSERVATION_DIM)
    states[:, FLOW_BLOCK_START:FLOW_BLOCK_END] = 0.0
    with torch.no_grad():
        logits_a, value_a = original(states)
        logits_b, value_b = migrated(states)
    assert torch.equal(logits_a, logits_b)
    assert torch.equal(value_a, value_b)


def test_migrated_policy_starts_blind_to_the_new_signals(tmp_path: Path) -> None:
    source, dest = tmp_path / "old.pt", tmp_path / "new.pt"
    _write_v16(source)
    migrate_flow_block_checkpoint(source, dest)
    migrated = ActorCritic.load(dest)

    weight = migrated.state_dict()["trunk.0.weight"]
    assert torch.count_nonzero(weight[:, FLOW_BLOCK_START:FLOW_BLOCK_END]) == 0
    assert torch.count_nonzero(weight[:, FLOW_BLOCK_END:]) > 0

    base = torch.rand(4, OBSERVATION_DIM)
    varied = base.clone()
    varied[:, FLOW_BLOCK_START:FLOW_BLOCK_END] = torch.rand(4, FLOW_BLOCK_END - FLOW_BLOCK_START)
    with torch.no_grad():
        assert torch.equal(migrated(base)[0], migrated(varied)[0])


def test_migration_refuses_other_versions_and_writes_nothing(tmp_path: Path) -> None:
    source, dest = tmp_path / "old.pt", tmp_path / "new.pt"
    ActorCritic().save(source)
    payload = torch.load(source, map_location="cpu", weights_only=True)
    for field, value in (
        ("observation_version", 15),
        ("action_space_version", ACTION_SPACE_VERSION - 1),
        ("policy_version", POLICY_VERSION - 1),
        ("obs_dim", OBSERVATION_DIM - 2),
    ):
        bad = dict(payload, observation_version=16)
        bad[field] = value
        torch.save(bad, source)
        assert migrate_flow_block_checkpoint(source, dest) is False
        assert not dest.exists()


def test_startup_carries_the_previous_canonical_forward(tmp_path: Path) -> None:
    _write_v16(tmp_path / _V16_NAME)

    _archive_old_canonicals(tmp_path)

    current = tmp_path / canonical_weights_filename()
    assert current.is_file()
    assert ActorCritic.load(current) is not None
    assert f"_o{OBSERVATION_VERSION}_" in current.name
    assert not (tmp_path / _V16_NAME).exists()
    assert (tmp_path / f"policy_legacy_a{ACTION_SPACE_VERSION}_o16_p{POLICY_VERSION}.pt").is_file()


def test_existing_current_canonical_is_never_overwritten(tmp_path: Path) -> None:
    _write_v16(tmp_path / _V16_NAME)
    current = tmp_path / canonical_weights_filename()
    ActorCritic().save(current)
    before = current.read_bytes()

    assert migrate_previous_observation_canonical(tmp_path) is False
    assert current.read_bytes() == before
