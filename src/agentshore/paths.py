"""Canonical filesystem paths for AgentShore runtime state.

No legacy fallback paths. A session that finds no state at the expected
paths behaves as a fresh init.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import platformdirs

GLOBAL_CONFIG_DIR: Final[Path] = Path(platformdirs.user_config_dir("agentshore", "SuperSwinkAI"))
GLOBAL_WEIGHTS_DIR: Final[Path] = GLOBAL_CONFIG_DIR / "weights"
GLOBAL_SESSIONS_DIR: Final[Path] = GLOBAL_CONFIG_DIR / "sessions"
GLOBAL_AVAILABILITY_PATH: Final[Path] = GLOBAL_CONFIG_DIR / "availability.yaml"
# Per-model token pricing; deep-merges over the wheel-bundled table. Edit + SIGHUP
# reprices every project's next dispatch with no restart. See pricing.load_pricebook.
GLOBAL_PRICING_PATH: Final[Path] = GLOBAL_CONFIG_DIR / "pricing.yaml"
# Per-agent-harness known-model catalog; each agent key's list wholesale-replaces
# the wheel-bundled default when present. See model_catalog.load_model_catalog.
GLOBAL_MODELS_PATH: Final[Path] = GLOBAL_CONFIG_DIR / "models.yaml"
# Machine-global user prefs (disabled non-critical plays, runtime timeouts), folded
# into every project's RuntimeConfig at load; SIGHUP/reload re-reads mid-session.
# Global-only by design — no per-project file. See agentshore.preferences.
GLOBAL_PREFERENCES_PATH: Final[Path] = GLOBAL_CONFIG_DIR / "preferences.yaml"

PROJECT_DIR_NAME: Final[str] = ".agentshore"


def project_dir(project_path: Path) -> Path:
    return project_path / PROJECT_DIR_NAME


def project_db_path(project_path: Path) -> Path:
    return project_dir(project_path) / "agentshore.db"


def project_context_file(project_path: Path) -> Path:
    return project_dir(project_path) / "context.json"


def project_archive_dir(project_path: Path) -> Path:
    return project_dir(project_path) / "archives"


def project_logs_dir(project_path: Path) -> Path:
    return project_dir(project_path) / "logs"


def project_learnings_file(project_path: Path) -> Path:
    return project_dir(project_path) / "learnings.json"


def project_reports_dir(project_path: Path) -> Path:
    return project_dir(project_path) / "reports"


def project_weights_dir(project_path: Path) -> Path:
    return project_dir(project_path) / "weights"
