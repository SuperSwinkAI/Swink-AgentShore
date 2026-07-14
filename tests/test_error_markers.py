"""Drift-pin for the single error-marker registry (``agentshore.error_markers``).

Mirrors ``tests/test_schema_fresh_db.py``: freeze the canonical shape and assert
every scoped view is a documented subset of it, so a future edit that widens one
auth spelling without the others — the exact drift this registry exists to
prevent — fails here instead of silently diverging across modules.
"""

from __future__ import annotations

import pytest

from agentshore.error_markers import (
    AUTH_MARKERS,
    CACHE_RENEWAL_MARKERS,
    ENOSPC_MARKERS,
    GIT_AUTH_FAILED_MARKERS,
    INVALID_MODEL_MARKERS,
    INVALID_MODEL_STDERR_PATTERNS,
    INVALID_MODEL_STDOUT_MARKERS,
    PROBE_NOT_AUTHED_MARKERS,
    PUBLISH_AUTH_MARKERS,
    RATE_LIMIT_MARKERS,
    RATE_LIMIT_STDERR_PATTERNS,
    RATE_LIMIT_STDOUT_MARKERS,
    STDERR_AUTH_PATTERNS,
    STDOUT_AUTH_MARKERS,
    TIMEOUT_MARKERS,
    TIMEOUT_STDERR_PATTERNS,
    TIMEOUT_STDOUT_MARKERS,
)
from agentshore.errors import GITHUB_AUTH_ERROR_MARKERS

# Frozen snapshot of the canonical auth superset. Update this deliberately when
# intentionally adding an auth spelling — the diff is the audit trail.
_FROZEN_AUTH_MARKERS = (
    "401 unauthorized",
    "403 forbidden",
    "active gh_token account lacks",
    "authentication",
    "bad credentials",
    "cannot access repository metadata",
    "connector repo 404",
    "could not resolve to a repository",
    "could not resolve to a repository with the name",
    "failed to refresh available models",
    "failed to renew cache ttl",
    "forbidden",
    "github connector returned 404",
    "http 401",
    "http 403",
    "invalid api key",
    "irrecoverable github access failure",
    "lacks access to repository",
    "not found/could not resolve repository",
    "not resolvable to this token/session",
    "repository is not resolvable to this token",
    "repository not found",
    "repository/pr is not accessible",
    "unauthorized",
)


def test_auth_markers_frozen_snapshot() -> None:
    assert frozenset(_FROZEN_AUTH_MARKERS) == AUTH_MARKERS


@pytest.mark.parametrize(
    "view",
    [
        GITHUB_AUTH_ERROR_MARKERS,
        PUBLISH_AUTH_MARKERS,
        STDERR_AUTH_PATTERNS,
        STDOUT_AUTH_MARKERS,
    ],
)
def test_scoped_auth_views_are_subsets(view: tuple[str, ...]) -> None:
    assert set(view) <= AUTH_MARKERS


def test_auth_markers_is_exactly_the_union_of_its_views() -> None:
    assert (
        frozenset(GITHUB_AUTH_ERROR_MARKERS).union(
            PUBLISH_AUTH_MARKERS, STDERR_AUTH_PATTERNS, STDOUT_AUTH_MARKERS
        )
        == AUTH_MARKERS
    )


def test_cache_renewal_is_nested_in_stderr_auth() -> None:
    # The watchdog subtracts CACHE_RENEWAL_MARKERS from STDERR_AUTH_PATTERNS at
    # runtime, so the renewal markers must actually be present in that view.
    assert set(STDERR_AUTH_PATTERNS) >= CACHE_RENEWAL_MARKERS


def test_probe_vocabularies_are_siblings_not_subsets() -> None:
    # The login-status / git-transport probe vocabularies detect the same
    # *category* (can't authenticate) via a disjoint string set; they are NOT
    # claimed to be subsets of AUTH_MARKERS. Assert they carry distinctive
    # markers absent from the GitHub-API auth superset, so a future edit that
    # accidentally collapses them into AUTH_MARKERS is caught.
    assert "not logged in" in set(PROBE_NOT_AUTHED_MARKERS) - AUTH_MARKERS
    assert "terminal prompts disabled" in set(GIT_AUTH_FAILED_MARKERS) - AUTH_MARKERS


@pytest.mark.parametrize(
    ("stderr_view", "stdout_view", "canonical"),
    [
        (RATE_LIMIT_STDERR_PATTERNS, RATE_LIMIT_STDOUT_MARKERS, RATE_LIMIT_MARKERS),
        (TIMEOUT_STDERR_PATTERNS, TIMEOUT_STDOUT_MARKERS, TIMEOUT_MARKERS),
        (INVALID_MODEL_STDERR_PATTERNS, INVALID_MODEL_STDOUT_MARKERS, INVALID_MODEL_MARKERS),
    ],
)
def test_canonical_set_is_union_of_stream_views(
    stderr_view: tuple[str, ...],
    stdout_view: tuple[str, ...],
    canonical: frozenset[str],
) -> None:
    assert set(stderr_view) <= canonical
    assert set(stdout_view) <= canonical
    assert canonical == frozenset(stderr_view).union(stdout_view)


def test_codex_usage_limit_markers_present_in_rate_limit_family() -> None:
    # #276: Codex's quota-miss signatures must live in the rate-limit family so a
    # weekly-quota exit classifies as RATE_LIMIT (like Claude's session limit) and
    # inherits the provider-wide eligibility hold + take_break, not UNKNOWN.
    assert "usage limit" in RATE_LIMIT_STDERR_PATTERNS
    assert "try again at" in RATE_LIMIT_STDERR_PATTERNS
    assert "hit your usage limit" in RATE_LIMIT_STDOUT_MARKERS


def test_enospc_markers_include_rust_os_error_spelling() -> None:
    # #332: Codex is Rust and prints "os error 28", not "errno 28" — the two
    # marker sets otherwise look identical but Codex's disk-full stderr ("No
    # space left on device (os error 28)") only ever matches the "os error 28"
    # spelling. Missing it let ENOSPC fall through to AUTH classification.
    assert "os error 28" in ENOSPC_MARKERS
    assert "errno 28" in ENOSPC_MARKERS
    assert "no space left on device" in ENOSPC_MARKERS


def test_consumers_read_the_registry_objects() -> None:
    # Phase 1 rewiring: the post-decomposition consumer names must resolve to the
    # exact registry objects, so the single home is real (not a re-copied table).
    from agentshore.agents.auth_probe import _NOT_AUTHED_MARKERS
    from agentshore.agents.cli.errors import _AUTH_PATTERNS, _AUTH_STDOUT, _RATE_LIMIT_PATTERNS
    from agentshore.agents.git_auth_probe import _AUTH_FAILED_MARKERS
    from agentshore.plays._publish_reconciler import _AUTH_ERROR_MARKERS

    assert _AUTH_PATTERNS is STDERR_AUTH_PATTERNS
    assert _AUTH_STDOUT is STDOUT_AUTH_MARKERS
    assert _RATE_LIMIT_PATTERNS is RATE_LIMIT_STDERR_PATTERNS
    assert _AUTH_ERROR_MARKERS is PUBLISH_AUTH_MARKERS
    assert _NOT_AUTHED_MARKERS is PROBE_NOT_AUTHED_MARKERS
    assert _AUTH_FAILED_MARKERS is GIT_AUTH_FAILED_MARKERS
