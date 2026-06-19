"""Canonical filesystem paths for AgentShore runtime state.

No legacy fallback paths. A session that finds no state at the expected
paths behaves as a fresh init.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import platformdirs

# ── Global (user-level) config dir ───────────────────────────────────────────
GLOBAL_CONFIG_DIR: Final[Path] = Path(platformdirs.user_config_dir("agentshore", "SuperSwinkAI"))
GLOBAL_WEIGHTS_DIR: Final[Path] = GLOBAL_CONFIG_DIR / "weights"
GLOBAL_SESSIONS_DIR: Final[Path] = GLOBAL_CONFIG_DIR / "sessions"
GLOBAL_AVAILABILITY_PATH: Final[Path] = GLOBAL_CONFIG_DIR / "availability.yaml"
# Single global touchpoint for per-model token pricing. When present it
# overrides (deep-merges over) the pricing table bundled in the wheel; editing
# it then sending SIGHUP repriced every project's next dispatch without a code
# change or restart. See ``agentshore.agents.pricing.load_pricebook``.
GLOBAL_PRICING_PATH: Final[Path] = GLOBAL_CONFIG_DIR / "pricing.yaml"
# Machine-global user preferences (e.g. disabled non-critical plays, runtime
# timeouts). Folded into every project's ``RuntimeConfig`` at load time, so a
# reload (SIGHUP / IPC reload-config) re-reads it mid-session. There is NO
# per-project preferences file — these are deliberately global-only. See
# ``agentshore.preferences``.
GLOBAL_PREFERENCES_PATH: Final[Path] = GLOBAL_CONFIG_DIR / "preferences.yaml"

# ── Per-project directory name ────────────────────────────────────────────────
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
