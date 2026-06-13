"""Build context: resolved paths + flags threaded through every phase."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .version import repo_root

if TYPE_CHECKING:
    from pathlib import Path

APP_NAME = "AgentShore"
APP_BUNDLE_ID = "ai.agentshore.desktop"


@dataclass
class BuildContext:
    """Everything a phase needs: paths, build mode, and platform flags."""

    root: Path
    build_mode: str = "release"  # "release" | "debug"

    # Shared flags
    skip_dashboard: bool = False
    skip_sidecar: bool = False
    no_sign: bool = False

    # macOS flags
    build_pkg: bool = True
    do_install: bool = False
    notarize: bool = False
    keychain_profile: str = "agentshore-notary"

    # Resolved at runtime
    app_signing_id: str = ""
    installer_signing_id: str = ""
    bundled_wheel: Path | None = None

    # Extra args forwarded verbatim to platform-specific tooling (e.g. Windows).
    extra: list[str] = field(default_factory=list)

    @property
    def desktop_dir(self) -> Path:
        return self.root / "desktop"

    @property
    def tauri_dir(self) -> Path:
        return self.desktop_dir / "src-tauri"

    @property
    def packaging_dir(self) -> Path:
        return self.root / "packaging" / "desktop"

    @property
    def bundle_macos_dir(self) -> Path:
        return self.tauri_dir / "target" / self.build_mode / "bundle" / "macos"

    @property
    def built_app(self) -> Path:
        return self.bundle_macos_dir / f"{APP_NAME}.app"

    @property
    def target_dir(self) -> Path:
        return self.tauri_dir / "target" / self.build_mode

    @property
    def is_signed(self) -> bool:
        return bool(self.app_signing_id)


def default_context() -> BuildContext:
    return BuildContext(root=repo_root())
