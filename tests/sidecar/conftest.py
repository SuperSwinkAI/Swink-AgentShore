"""Shared fixtures for sidecar tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from agentshore.agents import identity as identity_mod

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _offline_identity_token_gates() -> Iterator[None]:
    """Sidecar fixture identities name GitHub logins that aren't on this
    machine; keep session.start's token/repo-access gates offline. Tests that
    exercise a gate patch the real function back in."""
    with (
        patch.object(identity_mod, "report_identities", return_value=[]),
        patch.object(identity_mod, "report_identity_repo_access", return_value=[]),
    ):
        yield
