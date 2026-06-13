"""Shared CI skip marker for tests that need external tooling.

A handful of tests exercise the real ``bd``/beads binary or the ``sqlite3``
``.recover`` CLI. Neither is provisioned on the GitHub-hosted ubuntu runner, so
those tests fail there with "bd binary not found" / empty-recover errors even
though they pass everywhere the tooling exists (local dev, where ``bd`` is on
PATH and sqlite ships ``.recover``).

The GH CI workflow sets ``AGENTSHORE_SKIP_CI_UNSUPPORTED=1`` for the pytest step;
this marker skips the affected tests under that flag only. Locally the flag is
unset, so the tests run as usual and still catch real regressions — we are not
disabling them, only acknowledging an environment gap on the runner.
"""

from __future__ import annotations

import os
import shutil

import pytest

requires_external_tooling = pytest.mark.skipif(
    os.environ.get("AGENTSHORE_SKIP_CI_UNSUPPORTED") == "1",
    reason="requires bd / sqlite .recover tooling not provisioned on the GH CI runner",
)

# The sqlite3 ``.recover`` CLI ships with most Unix sqlite builds but is not on
# PATH on the GH runner (flag set) nor on a stock Windows box (no sqlite3.exe).
# Skip the recovery tests wherever the CLI is genuinely unavailable.
requires_sqlite3_cli = pytest.mark.skipif(
    os.environ.get("AGENTSHORE_SKIP_CI_UNSUPPORTED") == "1" or shutil.which("sqlite3") is None,
    reason="requires the sqlite3 .recover CLI on PATH",
)
