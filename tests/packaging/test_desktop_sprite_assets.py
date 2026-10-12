"""The desktop app must receive agent sprite PNGs it can actually serve.

The dashboard library build emits sprite PNGs via Rollup ``emitFile``. With
Vite's default ``base: "/"`` those come out as root-absolute ``"/x.png"``
strings baked into ``dist/index.js``; the desktop Vite build then has nothing
to re-emit and the Tauri WebView 404s on ``tauri://localhost/x.png``, so every
agent renders as the coloured placeholder box instead of its sprite. A
relative base makes the lib emit ``new URL("x.png", import.meta.url)``, which
the desktop build turns into hashed ``/assets/x-HASH.png`` files it ships.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_lib_build_uses_relative_base() -> None:
    config = (REPO_ROOT / "dashboard/vite.config.ts").read_text(encoding="utf-8")
    lib_branch = config[config.index('mode === "lib"') : config.index("const wsTarget")]
    assert re.search(r'base:\s*"\./"', lib_branch), (
        "dashboard lib build must set base: './' so sprite PNGs are emitted as "
        "new URL(..., import.meta.url) for the desktop build to re-emit"
    )
