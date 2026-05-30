"""Tests for `agentshore train` CLI command."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from click.testing import CliRunner

from agentshore.cli import main
from agentshore.data.store import (
    DataStore,
    ExperienceRecord,
    PlayRecord,
    SessionRecord,
)
from agentshore.rl.action_space import ACTION_SPACE_VERSION, NUM_ACTIONS
from agentshore.rl.observation import OBSERVATION_DIM

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _populate_experience(
    store: DataStore,
    session_id: str,
    num_records: int = 5,
) -> None:
    """Insert valid experience records for offline training."""
    now = _now_iso()
    await store.create_session(
        SessionRecord(
            session_id=session_id,
            project_path="/tmp/test-project",
            started_at=now,
            status="running",
        )
    )

    for i in range(num_records):
        play_id = await store.record_play(
            PlayRecord(
                session_id=session_id,
                play_type="issue_pickup",
                started_at=now,
                success=True,
                agent_id="agent-1",
                ended_at=now,
                duration_ms=1000,
                token_cost=100,
                dollar_cost=0.01,
            )
        )

        state = np.random.randn(OBSERVATION_DIM).astype(np.float32)
        next_state = np.random.randn(OBSERVATION_DIM).astype(np.float32)
        mask = np.ones(NUM_ACTIONS, dtype=np.float32)

        await store.record_experience(
            ExperienceRecord(
                session_id=session_id,
                play_id=play_id,
                state_vector=state.tobytes(),
                action=0,
                reward=0.5,
                next_state=next_state.tobytes(),
                done=0,
                old_log_prob=-1.0,
                value_estimate=0.3,
                action_mask=mask.tobytes(),
                policy_version="ppo-v1-test",
                action_space_version=ACTION_SPACE_VERSION,
                config_hash="test",
                step_index=i,
            )
        )


async def _make_populated_db(db_path: Path, num_sessions: int = 1, records_per: int = 5) -> None:
    """Create and populate a database at *db_path*."""
    store = DataStore(db_path)
    await store.initialize()
    try:
        for idx in range(num_sessions):
            await _populate_experience(store, f"session-{idx}", records_per)
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_train_happy_path(tmp_path: Path) -> None:
    """Populate DB with experience, run train, expect success and checkpoint."""
    db_path = tmp_path / "agentshore.db"
    out_path = tmp_path / "policy.pt"

    asyncio.run(_make_populated_db(db_path))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["train", "--sessions", str(db_path), "--output", str(out_path)],
    )
    assert result.exit_code == 0, f"CLI failed:\n{result.output}"
    assert out_path.exists(), "Checkpoint file was not created"
    assert "Saved checkpoint to" in result.output


def test_train_no_database(tmp_path: Path) -> None:
    """When --project has no .agentshore/agentshore.db, exit with error."""
    empty_project = tmp_path / "empty_project"
    empty_project.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["train", "--project", str(empty_project)],
    )
    assert result.exit_code != 0
    assert "database not found" in (result.output + (result.stderr or "")).lower() or (
        "Error" in result.output
    )


def test_train_empty_database(tmp_path: Path) -> None:
    """DB exists but has no experience rows -- should report no sessions."""
    db_path = tmp_path / "agentshore.db"

    async def _init_empty() -> None:
        store = DataStore(db_path)
        await store.initialize()
        await store.close()

    asyncio.run(_init_empty())

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["train", "--sessions", str(db_path), "--output", str(tmp_path / "out.pt")],
    )
    assert result.exit_code == 0, f"CLI failed:\n{result.output}"
    assert "No compatible sessions" in result.output


def test_train_custom_output_path(tmp_path: Path) -> None:
    """--output should write the checkpoint at the given path."""
    db_path = tmp_path / "agentshore.db"
    custom_out = tmp_path / "custom" / "model.pt"

    asyncio.run(_make_populated_db(db_path))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["train", "--sessions", str(db_path), "--output", str(custom_out)],
    )
    assert result.exit_code == 0, f"CLI failed:\n{result.output}"
    assert custom_out.exists(), f"Expected checkpoint at {custom_out}"


def test_train_warm_start(tmp_path: Path) -> None:
    """--source-policy loads an existing checkpoint instead of cold-starting."""
    from agentshore.rl.policy import ActorCritic

    db_path = tmp_path / "agentshore.db"
    source_pt = tmp_path / "source_policy.pt"
    out_path = tmp_path / "trained.pt"

    # Save a fresh policy to use as warm-start source
    policy = ActorCritic()
    policy.save(source_pt)

    asyncio.run(_make_populated_db(db_path))

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "train",
            "--sessions",
            str(db_path),
            "--source-policy",
            str(source_pt),
            "--output",
            str(out_path),
        ],
    )
    assert result.exit_code == 0, f"CLI failed:\n{result.output}"
    assert "Loaded policy from" in result.output
    assert out_path.exists()


def test_train_metrics_logged(tmp_path: Path) -> None:
    """Per-session training stats (policy_loss, value_loss, entropy) appear in output."""
    db_path = tmp_path / "agentshore.db"
    out_path = tmp_path / "policy.pt"

    asyncio.run(_make_populated_db(db_path))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["train", "--sessions", str(db_path), "--output", str(out_path)],
    )
    assert result.exit_code == 0, f"CLI failed:\n{result.output}"
    assert "policy_loss=" in result.output
    assert "value_loss=" in result.output
    assert "entropy=" in result.output


def test_train_multiple_sessions(tmp_path: Path) -> None:
    """Three sessions with experience -- verify all three are trained."""
    db_path = tmp_path / "agentshore.db"
    out_path = tmp_path / "policy.pt"

    asyncio.run(_make_populated_db(db_path, num_sessions=3, records_per=5))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["train", "--sessions", str(db_path), "--output", str(out_path)],
    )
    assert result.exit_code == 0, f"CLI failed:\n{result.output}"
    assert "Trained on 3 session(s)" in result.output
    # Each session should have a stats line
    assert result.output.count("session=") == 3


def test_train_custom_epochs(tmp_path: Path) -> None:
    """--epochs 2 should still succeed (overrides default of 4)."""
    db_path = tmp_path / "agentshore.db"
    out_path = tmp_path / "policy.pt"

    asyncio.run(_make_populated_db(db_path))

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "train",
            "--sessions",
            str(db_path),
            "--output",
            str(out_path),
            "--epochs",
            "2",
        ],
    )
    assert result.exit_code == 0, f"CLI failed:\n{result.output}"
    assert out_path.exists()


def test_train_checkpoint_loadable(tmp_path: Path) -> None:
    """After training, the saved checkpoint can be loaded with ActorCritic.load()."""
    from agentshore.rl.policy import ActorCritic

    db_path = tmp_path / "agentshore.db"
    out_path = tmp_path / "policy.pt"

    asyncio.run(_make_populated_db(db_path))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["train", "--sessions", str(db_path), "--output", str(out_path)],
    )
    assert result.exit_code == 0, f"CLI failed:\n{result.output}"
    assert out_path.exists()

    loaded = ActorCritic.load(out_path)
    assert loaded is not None
    # Verify forward pass works
    import torch

    obs = torch.randn(1, OBSERVATION_DIM)
    logits, value = loaded(obs)
    assert logits.shape == (1, NUM_ACTIONS)
    assert value.shape == (1, 1)
