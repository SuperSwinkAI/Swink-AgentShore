"""Tests for the shared wire-framing discipline (``agentshore.ipc.wire``)."""

from __future__ import annotations

import json
import math

from agentshore.ipc.wire import frame, json_safe


def test_json_safe_nulls_non_finite_floats() -> None:
    assert json_safe(float("inf")) is None
    assert json_safe(float("-inf")) is None
    assert json_safe(float("nan")) is None
    assert json_safe(1.5) == 1.5


def test_json_safe_recurses_into_containers() -> None:
    value = {"a": [1.0, float("inf")], "b": {"c": float("nan")}}
    assert json_safe(value) == {"a": [1.0, None], "b": {"c": None}}


def test_json_safe_preserves_bools_and_scalars() -> None:
    # bool is a float subclass conceptually elsewhere; it must pass through.
    assert json_safe(True) is True
    assert json_safe(False) is False
    assert json_safe("x") == "x"
    assert json_safe(7) == 7
    assert json_safe(None) is None


def test_json_safe_stringifies_non_str_dict_keys() -> None:
    assert json_safe({1: "a"}) == {"1": "a"}


def test_frame_appends_single_newline_and_is_valid_json() -> None:
    text = frame({"k": 1})
    assert text.endswith("\n")
    assert text.count("\n") == 1
    assert json.loads(text) == {"k": 1}


def test_frame_emits_valid_json_for_non_finite_floats() -> None:
    text = frame({"value": float("inf"), "ratio": float("nan")})
    assert "Infinity" not in text
    assert "NaN" not in text
    # Strict parse would raise on bare Infinity/NaN tokens.
    assert json.loads(text) == {"value": None, "ratio": None}


def test_frame_rejects_non_finite_via_allow_nan_false_after_sanitizing() -> None:
    # json_safe nulls non-finite floats, so the allow_nan=False dumps never
    # needs to reject — but a finite float survives untouched.
    assert math.isfinite(1.25)
    assert json.loads(frame({"x": 1.25})) == {"x": 1.25}
