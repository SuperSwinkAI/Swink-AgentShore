"""Allow running as ``python -m agentshore.sidecar``.

PyInstaller targets ``packaging/desktop/sidecar_entrypoint.py``, which calls
:func:`agentshore.sidecar.server.run` directly. This ``__main__`` exists so the
unfrozen package is invokable identically during development.
"""

from __future__ import annotations

from agentshore.sidecar.server import run

run()
