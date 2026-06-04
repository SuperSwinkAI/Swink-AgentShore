"""Interactive wizard for binding GitHub identities to coding-agent types.

Invoked from ``agentshore init`` after ``agentshore.yaml`` is generated, and
from ``agentshore identity --reconfigure`` to update an existing project's
bindings without resetting state. Detects gh-authenticated accounts, prompts
the user to bind one to each detected agent CLI, and patches the YAML in-place.

The concerns are split into :mod:`gh_accounts`, :mod:`keychain`,
:mod:`wizard`, :mod:`yaml_patch`, and :mod:`report`.
"""

from __future__ import annotations

from agentshore.identity_wizard.gh_accounts import detect_gh_accounts, looks_like_pat
from agentshore.identity_wizard.report import (
    echo_identity_report,
    echo_repo_access_report,
    run_identity_wizard,
)
from agentshore.identity_wizard.wizard import IdentityBinding, run_wizard

__all__ = [
    "IdentityBinding",
    "detect_gh_accounts",
    "echo_identity_report",
    "echo_repo_access_report",
    "looks_like_pat",
    "run_identity_wizard",
    "run_wizard",
]
