"""Tests for agentshore/__main__.py — `python -m agentshore` entry point."""

from __future__ import annotations

import runpy
import sys
from unittest.mock import MagicMock


def test_main_module_invokes_cli_main() -> None:
    """Running ``python -m agentshore`` must call agentshore.cli.main()."""
    fake_main = MagicMock()

    # __main__ does ``from agentshore.cli import main``; patch the attribute before
    # runpy re-executes the module body so the cached cli module resolves to our fake.
    import agentshore.cli as cli_module

    original = cli_module.main
    cli_module.main = fake_main  # type: ignore[assignment]

    # Drop any cached __main__ so runpy actually executes the module body.
    sys.modules.pop("agentshore.__main__", None)

    try:
        runpy.run_module("agentshore", run_name="__main__")
    finally:
        cli_module.main = original  # type: ignore[assignment]
        sys.modules.pop("agentshore.__main__", None)

    fake_main.assert_called_once_with()
