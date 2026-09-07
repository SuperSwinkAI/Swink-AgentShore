"""Generated wire-schema guards for agentshore.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.config import load_config
from agentshore.errors import ConfigError


@pytest.mark.parametrize(
    ("yaml_text", "unknown_path"),
    [
        ("budjet:\n  enabled: true\n", "budjet"),
        ("auto:\n  detect_agentz: true\n", "auto.detect_agentz"),
        ("circuit_breaker:\n  typo_seconds: 10\n", "circuit_breaker.typo_seconds"),
    ],
)
def test_unknown_wire_fields_are_rejected(
    tmp_path: Path, yaml_text: str, unknown_path: str
) -> None:
    path = tmp_path / "agentshore.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError, match=unknown_path):
        load_config(path)


def test_parser_does_not_define_a_second_typed_dict_schema() -> None:
    parser_source = (
        Path(__file__).parents[1] / "src" / "agentshore" / "config" / "_parsers.py"
    ).read_text(encoding="utf-8")

    assert "TypedDict" not in parser_source
    assert "class _Raw" not in parser_source
