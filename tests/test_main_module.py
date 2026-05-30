"""Tests for agentshore/__main__.py — `python -m agentshore` entry point."""

from __future__ import annotations

import runpy
import sys
from unittest.mock import MagicMock


def test_main_module_invokes_cli_main() -> None:
    """Running ``python -m agentshore`` must call agentshore.cli.main()."""
    fake_main = MagicMock()

    # Patch the cli.main attribute that __main__ imports. Because __main__.py
    # does ``from agentshore.cli import main`` and then calls ``main()``, we need
    # to replace the function object on the agentshore.cli module *before* the
    # __main__ module body executes. runpy.run_module re-executes __main__
    # with run_name="__main__"; the import of agentshore.cli inside that fresh
    # execution will resolve to the already-imported module in sys.modules,
    # so monkey-patching the attribute beforehand is sufficient.
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
