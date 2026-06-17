"""Tests for machine-global user preferences: file IO, allowlist, config merge,
and the USER_DISABLED mask overlay (honored even by the reverse failsafe)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore import preferences as gp
from agentshore.config import PreferencesConfig, RuntimeConfig, load_config
from agentshore.rl.action_space import PLAY_TO_INDEX
from agentshore.rl.mask import compute_action_mask, compute_mask_reasons
from agentshore.rl.mask_reason import USER_DISABLED, MaskSource
from agentshore.state import AgentType, PlayType

# Reuse the well-exercised state builders from the mask test module.
from tests.test_rl_mask import (  # noqa: E402
    _agent_snapshot,
    _issue_snapshot,
    _registry_all_false,
    _registry_all_true,
    _state,
)


@pytest.fixture
def global_prefs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the global preferences file to a temp path."""
    path = tmp_path / "preferences.yaml"
    monkeypatch.setattr(gp, "GLOBAL_PREFERENCES_PATH", path)
    return path


# --------------------------------------------------------------------------- #
# Allowlist + validation
# --------------------------------------------------------------------------- #


def test_allowlist_excludes_delivery_and_selfheal_plays() -> None:
    allowed = set(gp.disableable_play_values())
    assert allowed == {"cleanup", "design_audit", "groom_backlog", "prune", "run_qa"}
    for critical in ("issue_pickup", "code_review", "merge_pr", "reconcile_state", "end_session"):
        assert critical not in allowed


def test_validate_disabled_plays_accepts_allowlisted_dedupes_and_orders() -> None:
    assert gp.validate_disabled_plays(["run_qa", "cleanup", "run_qa"]) == ("cleanup", "run_qa")


def test_validate_disabled_plays_rejects_non_allowlisted() -> None:
    with pytest.raises(gp.PreferencesError) as exc:
        gp.validate_disabled_plays(["cleanup", "issue_pickup"])
    assert "issue_pickup" in str(exc.value)


def test_coerce_drops_unknown_and_critical_entries() -> None:
    # Lenient on-disk coercion silently drops anything not allowlisted.
    assert gp._coerce_disabled_plays(["cleanup", "issue_pickup", "bogus", 7]) == ("cleanup",)
    assert gp._coerce_disabled_plays("not-a-list") == ()


# --------------------------------------------------------------------------- #
# File round-trip
# --------------------------------------------------------------------------- #


def test_save_load_round_trip(global_prefs: Path) -> None:
    gp.save_preferences_data({"disabled_plays": ("run_qa", "prune")})
    assert global_prefs.exists()
    assert gp.load_preferences_data()["disabled_plays"] == ("prune", "run_qa")


def test_load_missing_file_returns_defaults(global_prefs: Path) -> None:
    assert not global_prefs.exists()
    assert gp.load_preferences_data() == {"disabled_plays": ()}


def test_load_malformed_file_returns_defaults(global_prefs: Path) -> None:
    global_prefs.write_text("{ this is: not valid yaml ::::", encoding="utf-8")
    assert gp.load_preferences_data() == {"disabled_plays": ()}


# --------------------------------------------------------------------------- #
# Config merge
# --------------------------------------------------------------------------- #


def test_load_config_folds_in_global_preferences(global_prefs: Path) -> None:
    gp.save_preferences_data({"disabled_plays": ("cleanup",)})
    cfg = load_config(None)
    assert cfg.preferences.disabled_plays == ("cleanup",)


def test_load_config_defaults_with_no_preferences_file(global_prefs: Path) -> None:
    cfg = load_config(None)
    assert cfg.preferences.disabled_plays == ()


# --------------------------------------------------------------------------- #
# Mask overlay
# --------------------------------------------------------------------------- #


def _cfg(disabled: tuple[str, ...]) -> RuntimeConfig:
    return RuntimeConfig(preferences=PreferencesConfig(disabled_plays=disabled))


def test_disabled_play_is_masked_and_others_unaffected() -> None:
    base = compute_action_mask(_state(), _registry_all_true())
    assert base[PLAY_TO_INDEX[PlayType.CLEANUP]]  # enabled in the control

    mask = compute_action_mask(_state(), _registry_all_true(), cfg=_cfg(("cleanup",)))
    assert not mask[PLAY_TO_INDEX[PlayType.CLEANUP]]
    # A different allowlisted play stays available.
    assert mask[PLAY_TO_INDEX[PlayType.PRUNE]]


def test_disabled_play_reason_is_user_disabled() -> None:
    reasons = compute_mask_reasons(_state(), _registry_all_true(), cfg=_cfg(("cleanup",)))
    assert reasons[PlayType.CLEANUP] is USER_DISABLED
    assert reasons[PlayType.CLEANUP].source is MaskSource.PREFERENCE


def test_hand_edited_critical_play_is_not_masked_by_overlay() -> None:
    # Defense-in-depth: even if a critical play leaks into the config (e.g. a
    # hand-edited file bypassing the write-boundary validator), the mask overlay
    # re-checks the allowlist and refuses to disable it.
    cfg = _cfg(("issue_pickup",))
    reasons = compute_mask_reasons(_state(), _registry_all_true(), cfg=cfg)
    assert reasons.get(PlayType.ISSUE_PICKUP) is not USER_DISABLED


def test_reverse_failsafe_does_not_resurrect_a_disabled_play() -> None:
    state = _state(
        open_issues=[_issue_snapshot(234)],
        agents=[_agent_snapshot("codex-1", AgentType.CODEX)],
    )
    mask = compute_action_mask(
        state,
        _registry_all_false(),
        cfg=_cfg(("run_qa",)),
        apply_reverse_failsafe=True,
    )
    assert mask.any()  # the failsafe still opened a fallback menu
    assert mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]  # other work was lifted
    assert not mask[PLAY_TO_INDEX[PlayType.RUN_QA]]  # user choice honored over failsafe
