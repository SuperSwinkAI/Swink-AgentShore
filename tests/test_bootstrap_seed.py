"""Guards for the shipped warm-start seed (``agentshore.data/bootstrap_policy.pt``).

The seed lets fresh installs start from a trained policy instead of cold-starting
(``_resolve_policy_path`` step 4). It is version-pinned: ``ActorCritic.load`` hard-fails
on any action-space / policy / observation version drift, so a stale seed would silently
become dead weight. These tests fail the moment that happens, forcing a re-export via
``make export-bootstrap-policy``.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from agentshore.rl.policy import ActorCritic


def _seed_path() -> Path:
    return Path(str(importlib.resources.files("agentshore.data") / "bootstrap_policy.pt"))


def test_bootstrap_seed_is_shipped() -> None:
    seed = _seed_path()
    assert seed.is_file(), (
        "bootstrap_policy.pt is missing — fresh installs would cold-start. "
        "Regenerate it with `make export-bootstrap-policy`."
    )


def test_bootstrap_seed_matches_build_versions() -> None:
    model = ActorCritic.load(_seed_path())
    assert model is not None


def test_bootstrap_seed_is_roster_portable() -> None:
    model = ActorCritic.load(_seed_path())
    assert model.num_configs == 0, (
        "shipped seed carries a config head — re-export without --keep-config-head "
        "so it is portable across agent rosters."
    )
