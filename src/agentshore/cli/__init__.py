"""AgentShore CLI entry point.

Subcommand implementations live in :mod:`agentshore.cli.commands` and shared
helpers live in sibling modules under :mod:`agentshore.cli`. The public
surface — the ``main`` Click group plus the helpers that downstream tests
import directly — is re-exported here, so ``from agentshore.cli import
<name>`` works for every legacy name.
"""

from __future__ import annotations

import uuid

import click

from agentshore import __version__
from agentshore.cli.agent_select import (
    _agent_key_for_detected_binary,
    _interactive_agent_select,
    _load_config_for_agent_setup,
    _needs_interactive_agent_selection,
)

# ---------------------------------------------------------------------------
# Subcommand imports
# ---------------------------------------------------------------------------
from agentshore.cli.commands.archive import (
    archive,
    archive_compare,
    archive_create,
    archive_list,
)
from agentshore.cli.commands.configure import configure
from agentshore.cli.commands.dashboard import dashboard
from agentshore.cli.commands.identity import identity
from agentshore.cli.commands.init import _reset_agentshore_database, _run_beads_init, init
from agentshore.cli.commands.report import report
from agentshore.cli.commands.start import start
from agentshore.cli.commands.stop import (
    _generate_end_session_report_cli,
    _wait_for_session_exit,
    stop,
)
from agentshore.cli.commands.train import train
from agentshore.cli.commands.trusted_ids import (
    _canonicalize_cli_github_login,
    _read_trusted_ids_config,
    _trusted_ids_config_path,
    _write_trusted_ids_config,
    trusted_ids,
    trusted_ids_add_gh,
    trusted_ids_add_pr,
    trusted_ids_list,
    trusted_ids_remove_gh,
    trusted_ids_remove_pr,
)

# ---------------------------------------------------------------------------
# Re-exports of constants (tests import these directly)
# ---------------------------------------------------------------------------
from agentshore.cli.constants import (
    _AGENT_KEY_BY_BINARY,
    _BYPASS_FLAGS,
    _CUSTOM_MODEL_SENTINEL,
    _DRAIN_WAIT_POLL_INTERVAL_S,
    _DRAIN_WAIT_RETRIES,
    _DRAIN_WAIT_TIMEOUT_S,
    _SOCKET_POLL_INTERVAL_S,
    _SOCKET_WAIT_RETRIES,
    _SOCKET_WAIT_TIMEOUT_S,
    _START_MODE_AGENT,
    _START_MODE_TUI,
    _SUPPORTED_CLI_AGENT_KEYS,
)

# ---------------------------------------------------------------------------
# Re-exports of helpers (tests import these directly)
# ---------------------------------------------------------------------------
from agentshore.cli.helpers import (
    _check_ssh_signing_key_loaded,
    _display_run_mode,
    _drain_wait_timeout_label,
    _install_loop_signal_handler,
    _int_or_none,
    _prepare_session_discovery_paths,
    _resolve_policy_mode_override,
    _resolve_start_run_mode,
    _str_or_none,
    _track_background_task,
)
from agentshore.cli.identity_helpers import (
    _agent_keys_from_yaml,
    _existing_identities_from_yaml,
    _identity_defaults_from_yaml,
    _identity_repo_name_with_owner,
)
from agentshore.cli.runtime import (
    _dispatch_command,
    _find_free_dashboard_port,
    _launch_dashboard_background,
    _logger,
    _run_agent_mode,
    _run_headless_mode,
    _run_solo_mode,
    _start_dashboard_bridge,
)
from agentshore.cli.seed import _resolve_seed_input_path

# ``_generate_default_config`` is defined in ``cli_helpers.py`` but several
# tests import it via ``from agentshore.cli import _generate_default_config``.
# Other names are re-exported so command modules can resolve them at call
# time through the ``agentshore.cli`` namespace (and tests can patch them
# there, as they did before the package split).
from agentshore.cli_helpers import (
    _detect_agents,
    _detect_api_keys,
    _detect_gh_remote,
    _ensure_gitignore_entry,
    _find_repo_root,
    _generate_default_config,
    _get_db_path,
    _render_or_merge_agentshore_yaml,
)

# Seed constants moved to the neutral ``agentshore.seed_input`` module (shared
# by the CLI and the bootstrap config fallback); still surfaced from
# ``agentshore.cli`` because tests import them here.
from agentshore.seed_input import _SEED_DIR_MAX_TOTAL_BYTES, _SEED_SUPPORTED_SUFFIXES

# ---------------------------------------------------------------------------
# Click root group — exposed as ``agentshore.cli.main`` for the pyproject entry
# point and ``from agentshore.cli import main`` test imports.  Subcommands are
# attached directly here; the legacy structure used ``@main.command()`` for
# the same effect, but inline attachment keeps the new package modules from
# importing back into ``__init__`` (which would create a cycle).
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(__version__, prog_name="agentshore")
def main() -> None:
    """AgentShore -- RL-based multi-agent coding orchestrator."""


main.add_command(start)
main.add_command(init)
main.add_command(configure)
main.add_command(identity)
main.add_command(trusted_ids)
main.add_command(report)
main.add_command(train)
main.add_command(archive)
main.add_command(dashboard)
main.add_command(stop)


__all__ = [
    "_AGENT_KEY_BY_BINARY",
    "_BYPASS_FLAGS",
    "_CUSTOM_MODEL_SENTINEL",
    "_DRAIN_WAIT_POLL_INTERVAL_S",
    "_DRAIN_WAIT_RETRIES",
    "_DRAIN_WAIT_TIMEOUT_S",
    "_SEED_DIR_MAX_TOTAL_BYTES",
    "_SEED_SUPPORTED_SUFFIXES",
    "_SOCKET_POLL_INTERVAL_S",
    "_SOCKET_WAIT_RETRIES",
    "_SOCKET_WAIT_TIMEOUT_S",
    "_START_MODE_AGENT",
    "_START_MODE_TUI",
    "_SUPPORTED_CLI_AGENT_KEYS",
    "_agent_key_for_detected_binary",
    "_agent_keys_from_yaml",
    "_canonicalize_cli_github_login",
    "_check_ssh_signing_key_loaded",
    "_detect_agents",
    "_detect_api_keys",
    "_detect_gh_remote",
    "_display_run_mode",
    "_dispatch_command",
    "_drain_wait_timeout_label",
    "_ensure_gitignore_entry",
    "_existing_identities_from_yaml",
    "_find_free_dashboard_port",
    "_find_repo_root",
    "_generate_default_config",
    "_generate_end_session_report_cli",
    "_get_db_path",
    "_identity_defaults_from_yaml",
    "_identity_repo_name_with_owner",
    "_install_loop_signal_handler",
    "_int_or_none",
    "_interactive_agent_select",
    "_launch_dashboard_background",
    "_load_config_for_agent_setup",
    "_logger",
    "_needs_interactive_agent_selection",
    "_prepare_session_discovery_paths",
    "_read_trusted_ids_config",
    "_render_or_merge_agentshore_yaml",
    "_reset_agentshore_database",
    "_resolve_policy_mode_override",
    "_resolve_seed_input_path",
    "_resolve_start_run_mode",
    "_run_agent_mode",
    "_run_beads_init",
    "_run_headless_mode",
    "_run_solo_mode",
    "_start_dashboard_bridge",
    "_str_or_none",
    "_track_background_task",
    "_trusted_ids_config_path",
    "_wait_for_session_exit",
    "_write_trusted_ids_config",
    "archive",
    "archive_compare",
    "archive_create",
    "archive_list",
    "configure",
    "dashboard",
    "identity",
    "init",
    "main",
    "report",
    "start",
    "stop",
    "train",
    "trusted_ids",
    "trusted_ids_add_gh",
    "trusted_ids_add_pr",
    "trusted_ids_list",
    "trusted_ids_remove_gh",
    "trusted_ids_remove_pr",
    "uuid",
]
