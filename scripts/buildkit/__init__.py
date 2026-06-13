"""AgentShore cross-platform build spine.

The shared, OS-agnostic build logic both `scripts/build-macos.sh` and
`scripts/build-windows.ps1` delegate to. See
`docs/design/build-pipeline-unification.md` for the design.

Currently exposes the version single-source-of-truth tooling
(`python -m scripts.buildkit version --check|--write`); the full build phases
are landed incrementally.
"""

from __future__ import annotations
