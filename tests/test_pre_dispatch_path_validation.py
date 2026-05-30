"""Pre-dispatch worktree-path validation (desktop-4ugk part 2).

If the agent's working directory contains a literal backslash-space,
``_dispatch_play`` must refuse to spawn the subprocess. Same check
applies to ``params.extras['worktree_path']`` when the play renders a
worktree pointer into the prompt.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from agentshore.plays.base import PlayParams
from agentshore.state import PlayType


def _events_from_caplog(records: list[logging.LogRecord]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    std_fields = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()) | {
        "message",
        "asctime",
    }
    for r in records:
        msg = r.msg
        if isinstance(msg, dict):
            out.append(msg)
            continue
        if hasattr(r, "event"):
            out.append(
                {
                    k: v
                    for k, v in r.__dict__.items()
                    if k not in std_fields and not k.startswith("_")
                }
            )
    return out


def _build_dispatch_harness(working_dir: Path) -> object:
    """Return an Orchestrator stub with just enough surface for ``_dispatch_play``.

    The non-test path through ``_dispatch_play`` reaches several helper
    methods; we mock the ones that would otherwise pull DB rows or talk to
    the executor. The branch we exercise is the early ``path_contains_backslash_space``
    rejection, so the rest never runs.
    """
    from agentshore.core.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._session_id = "test-pre-dispatch"
    orch._repo_root = Path("/tmp/fake-repo")
    orch._default_branch = "main"
    orch._pre_play_branches = {}
    orch._main_repo_dispatch_paused = False
    orch._draining = False
    orch._stop_requested = False
    orch._end_session_dispatch_started = False
    orch._in_flight = {}
    orch._dispatch_ctx = {}
    orch._first_play_override = None
    orch._override_queue = asyncio.Queue()
    orch._pending_override_kind = None
    orch._registry = None
    orch._selector = None
    orch._last_selection_digest = None

    # The drop helper writes to the store + emits a warning. We capture by
    # patching the method directly.
    orch._drop_selected_play_before_dispatch = AsyncMock()  # type: ignore[method-assign]

    manager_mock = MagicMock()
    manager_mock._working_dir = working_dir
    orch._manager = manager_mock  # type: ignore[assignment]

    # State stub for revalidation. Won't be reached on the early refuse path.
    state_mock = MagicMock()
    state_mock.session_state = MagicMock()
    state_mock.agents = []
    orch._state_provider = MagicMock()
    return orch, state_mock


@pytest.mark.asyncio
async def test_dispatch_refuses_backslash_space_working_dir(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad_dir = Path("/Users/example/Dev/Some\\ Project")
    orch, state = _build_dispatch_harness(bad_dir)
    params = PlayParams()

    with (
        structlog.testing.capture_logs() as captured_raw,
        caplog.at_level(logging.INFO, logger="agentshore.core"),
    ):
        result = await orch._dispatch_play(
            PlayType.CODE_REVIEW,
            params,
            state,
            revalidate=False,
        )
    captured = captured_raw if captured_raw else _events_from_caplog(list(caplog.records))

    assert result is False
    # The drop helper got the right event + reason.
    orch._drop_selected_play_before_dispatch.assert_awaited_once()
    drop_call = orch._drop_selected_play_before_dispatch.await_args
    assert drop_call.kwargs["event"] == "pre_dispatch_worktree_path_invalid"
    assert drop_call.kwargs["reason"] == "worktree_path_backslash_space"
    event_names = [str(e.get("event", "")) for e in captured]
    assert "pre_dispatch_worktree_path_invalid" in event_names


@pytest.mark.asyncio
async def test_dispatch_refuses_backslash_space_in_extras_worktree(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Even when the manager's working_dir is clean, an extras-supplied
    worktree path with backslash-space must be rejected."""
    orch, state = _build_dispatch_harness(tmp_path)
    params = PlayParams(extras={"worktree_path": "/tmp/Bad\\ Worktree"})

    with (
        structlog.testing.capture_logs() as captured_raw,
        caplog.at_level(logging.INFO, logger="agentshore.core"),
    ):
        result = await orch._dispatch_play(
            PlayType.ISSUE_PICKUP,
            params,
            state,
            revalidate=False,
        )
    captured = captured_raw if captured_raw else _events_from_caplog(list(caplog.records))

    assert result is False
    orch._drop_selected_play_before_dispatch.assert_awaited_once()
    drop_call = orch._drop_selected_play_before_dispatch.await_args
    assert drop_call.kwargs["reason"] == "worktree_path_backslash_space"
    event_names = [str(e.get("event", "")) for e in captured]
    assert "pre_dispatch_worktree_path_invalid" in event_names


@pytest.mark.asyncio
async def test_dispatch_proceeds_when_path_is_clean(tmp_path: Path) -> None:
    """Sanity: a clean path passes the backslash-space check.

    We only verify that the early-refuse branch is NOT taken; the rest of
    ``_dispatch_play`` raises on the missing executor/state plumbing, which
    is fine — we are only pinning the new guard's negative case.
    """
    orch, state = _build_dispatch_harness(tmp_path)
    params = PlayParams()

    # Stub minimum extras to fall through to revalidate=False path. We
    # short-circuit by asserting the helper itself is NOT called.
    state.session_state = MagicMock()
    # _shutdown_allows_only_end_agent reads draining/stop_requested/state.session_state.
    # All False by setup. The next interesting check after our guard is
    # END_SESSION revalidation — which doesn't fire for ISSUE_PICKUP.
    # _dispatch_play will then try to consume_pending / create the task, which
    # we don't care about — we only care that the new guard did NOT trip.
    from contextlib import suppress

    with suppress(Exception):
        await orch._dispatch_play(
            PlayType.ISSUE_PICKUP,
            params,
            state,
            revalidate=False,
        )

    # Critical: the bad-path guard was NOT invoked.
    orch._drop_selected_play_before_dispatch.assert_not_called()
