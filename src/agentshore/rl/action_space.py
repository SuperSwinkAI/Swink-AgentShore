"""V1 action space constants.

The tensor shape and slot order are locked. A reserved slot may be filled in
place without bumping ACTION_SPACE_VERSION so existing learned weights load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from agentshore.state import PlayType

if TYPE_CHECKING:
    from collections.abc import Mapping

# Declaration order of PlayType enum IS the canonical V1 action ordering.
V1_ACTION_ORDER: Final[tuple[PlayType, ...]] = tuple(PlayType)
NUM_ACTIONS: Final[int] = 22
ACTION_SPACE_VERSION: Final[int] = 13

# Sanity-checked at import time — guards against accidental enum reordering.
if len(V1_ACTION_ORDER) != NUM_ACTIONS:
    msg = f"V1_ACTION_ORDER has {len(V1_ACTION_ORDER)} entries, expected {NUM_ACTIONS}"
    raise ValueError(msg)

PLAY_TO_INDEX: Final[Mapping[PlayType, int]] = {pt: i for i, pt in enumerate(V1_ACTION_ORDER)}
INDEX_TO_PLAY: Final[Mapping[int, PlayType]] = {i: pt for i, pt in enumerate(V1_ACTION_ORDER)}
RESERVED_PLAYS: Final[frozenset[PlayType]] = frozenset(
    {PlayType.FUTURE_4, PlayType.FUTURE_7, PlayType.FUTURE_8}
)
