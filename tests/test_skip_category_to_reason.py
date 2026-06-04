"""Unit tests for the shared executor skip-category -> reason table (TNQA 03 L1).

``skip_category_to_reason`` is the single source for translating the executor's
``skip_category`` vocabulary into the unified ``PlaySkipReason`` surface. Before
03 L1 this mapping was an inline if/elif inside ``CompletionProcessor``.
"""

from __future__ import annotations

import pytest

from agentshore.core.mixins.completion import skip_category_to_reason


@pytest.mark.parametrize(
    ("skip_category", "expected"),
    [
        ("masked", "all_masked"),
        ("invalid_config", "all_masked"),
        ("no_target", "no_eligible_targets"),
        ("staffing", "no_eligible_targets"),
        ("selector_none", "selector_returned_none"),
        ("anything_else", "selector_returned_none"),
        ("", "selector_returned_none"),
        (None, "selector_returned_none"),
    ],
)
def test_skip_category_to_reason(skip_category: str | None, expected: str) -> None:
    assert skip_category_to_reason(skip_category) == expected
