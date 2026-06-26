#!/usr/bin/env python3
"""Mock coding-agent binary for testing.

Invoked as a subprocess by the CLI adapter tests. Behaviour is controlled by
environment variables so it can be driven by test fixtures without modifying
the argument interface that the real adapters use.

Environment variables
---------------------
MOCK_AGENT_MODE
    success (default) | failure | timeout | malformed | multi_block
MOCK_AGENT_FORMAT
    plain (default) | stream_json | codex_json
    Controls output format:
      - plain: just print a JSON result block
      - stream_json: emit Claude-style NDJSON events including token metadata
      - codex_json: emit Codex-style JSONL events including token metadata
MOCK_AGENT_DELAY_S
    Float seconds to sleep before producing output (default 0).
    timeout mode ignores this and sleeps indefinitely.
MOCK_AGENT_PR_NUMBER
    Integer PR number to include in the success artifact (default 42).
MOCK_AGENT_ISSUE_NUMBER
    Integer issue number to reference in the success result (default 1).
MOCK_AGENT_LINE_BYTES
    For mode=long_line: integer byte length of a single oversized line emitted
    before the success result. Used to exercise the asyncio readline limit.
"""

from __future__ import annotations

import json
import os
import sys
import time

_MODE = os.environ.get("MOCK_AGENT_MODE", "success")
_FORMAT = os.environ.get("MOCK_AGENT_FORMAT", "plain")
_DELAY = float(os.environ.get("MOCK_AGENT_DELAY_S", "0"))
_PR_NUMBER = int(os.environ.get("MOCK_AGENT_PR_NUMBER", "42"))
_ISSUE_NUMBER = int(os.environ.get("MOCK_AGENT_ISSUE_NUMBER", "1"))
_LINE_BYTES = int(os.environ.get("MOCK_AGENT_LINE_BYTES", "0"))


def _emit(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _success_result() -> dict[str, object]:
    return {
        "schema_version": 1,
        "success": True,
        "artifacts": [
            {
                "type": "pull_request",
                "number": _PR_NUMBER,
                "url": f"https://github.com/org/repo/pull/{_PR_NUMBER}",
            }
        ],
        "issues_created": [
            {
                "number": _ISSUE_NUMBER,
                "title": "Mock issue",
                "url": f"https://github.com/org/repo/issues/{_ISSUE_NUMBER}",
            }
        ],
        "requested_mutations": [],
        "metrics": {},
        "error": None,
    }


def _failure_result() -> dict[str, object]:
    return {
        "schema_version": 1,
        "success": False,
        "artifacts": [],
        "issues_created": [],
        "requested_mutations": [],
        "metrics": {},
        "error": "mock agent intentional failure",
    }


def _emit_plain(result: dict[str, object]) -> None:
    _emit("```json\n")
    _emit(json.dumps(result, indent=2))
    _emit("\n```\n")


def _emit_stream_json(result: dict[str, object]) -> None:
    """Emit Claude-style stream-json events followed by a final content block."""
    _emit(json.dumps({"type": "message_start", "message": {"id": "msg_mock"}}) + "\n")
    content = json.dumps(result, indent=2)
    _emit(
        json.dumps(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": f"```json\n{content}\n```\n"},
            }
        )
        + "\n"
    )
    # Usage event: token/cost metadata.
    _emit(
        json.dumps(
            {
                "type": "message_delta",
                "usage": {"input_tokens": 500, "output_tokens": 200},
            }
        )
        + "\n"
    )
    _emit(json.dumps({"type": "message_stop"}) + "\n")


def _emit_codex_json(result: dict[str, object]) -> None:
    """Emit Codex-style JSONL events with a final agent_message item."""
    content = f"```json\n{json.dumps(result, indent=2)}\n```"
    _emit(json.dumps({"type": "thread.started", "thread_id": "thread_mock"}) + "\n")
    _emit(json.dumps({"type": "turn.started"}) + "\n")
    _emit(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": content},
            }
        )
        + "\n"
    )
    _emit(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 300,
                    "cached_input_tokens": 0,
                    "output_tokens": 120,
                    "reasoning_output_tokens": 30,
                },
            }
        )
        + "\n"
    )


def _emit_result(result: dict[str, object]) -> None:
    if _FORMAT == "codex_json":
        _emit_codex_json(result)
    elif _FORMAT == "stream_json":
        _emit_stream_json(result)
    else:
        _emit_plain(result)


def main() -> None:
    if _DELAY > 0 and _MODE != "timeout":
        time.sleep(_DELAY)

    if _MODE == "timeout":
        # Sleep forever — the caller will send SIGTERM.
        while True:
            time.sleep(60)

    elif _MODE == "failure":
        _emit_result(_failure_result())
        sys.exit(0)

    elif _MODE == "malformed":
        _emit("this is not json\n")
        _emit("{invalid json\n")
        sys.exit(0)

    elif _MODE == "long_line":
        # Oversized line before the success result: exercises the asyncio readline cap.
        size = _LINE_BYTES if _LINE_BYTES > 0 else 200_000
        _emit("x" * size + "\n")
        _emit_result(_success_result())
        sys.exit(0)

    elif _MODE == "multi_block":
        # Emit an example block first, then the real result — parser should use the last one.
        _emit("Here is an example output:\n")
        _emit_plain(
            {
                "schema_version": 1,
                "success": True,
                "artifacts": [{"type": "example", "number": 0, "url": "example"}],
                "issues_created": [],
                "requested_mutations": [],
                "metrics": {},
                "error": None,
            }
        )
        _emit("\nAnd here is the actual result:\n")
        _emit_plain(_success_result())

    else:  # success
        _emit_result(_success_result())

    sys.exit(0)


if __name__ == "__main__":
    main()
