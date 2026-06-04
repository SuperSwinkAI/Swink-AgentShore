from __future__ import annotations

from typing import Final

STAGNATION_ENTROPY_MULTIPLIER: Final[float] = 1.5

# Saturation cap for ``open_pr_count`` when converting it to a [0, 1] ratio.
# Shared by the observation PR-pressure features and the reward PR-pressure
# bonus so the obs feature and the reward gradient always agree on "full".
SAT_OPEN_PRS_COUNT: Final[float] = 10.0
