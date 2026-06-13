"""AgentShore cross-platform build spine.

The desktop build entrypoint — run from the repo root:

    uv run python -m scripts.buildkit macos     # build the signed .app/.dmg/.pkg
    uv run python -m scripts.buildkit windows   # build the Inno Setup installer
    uv run python -m scripts.buildkit version --check|--write
    uv run python -m scripts.buildkit verify --target macos --app <AgentShore.app>

See `docs/design/build-pipeline-unification.md` for the design.
"""

from __future__ import annotations
