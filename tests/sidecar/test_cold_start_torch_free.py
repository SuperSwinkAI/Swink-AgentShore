"""Cold-start guard: torch must not be imported by sidecar startup.

DESIGN §8 (desktop-c8i.8) requires the desktop sidecar's cold start to
avoid pulling PyTorch into ``sys.modules``. The RL training stack
imports torch eagerly, but the JSON-RPC sidecar only routes commands —
torch should not transitively load before ``Orchestrator.start`` runs.

This test runs a fresh Python subprocess, imports the modules an
unfrozen sidecar boot would touch (server, handshake, recents, project,
identities, agents, archive_rpc, esr, config, embedded_bridge, build_id),
and asserts ``'torch' not in sys.modules``. The subprocess isolation is
load-bearing — the pytest process itself already has torch imported
because the RL test fixtures use it.
"""

from __future__ import annotations

import subprocess
import sys

_SIDECAR_BOOT_MODULES = (
    "agentshore.sidecar.server",
    "agentshore.sidecar.handshake",
    "agentshore.sidecar.recents",
    "agentshore.sidecar.project",
    "agentshore.sidecar.identities",
    "agentshore.sidecar.agents",
    "agentshore.sidecar.archive_rpc",
    "agentshore.sidecar.esr",
    "agentshore.sidecar.config",
    "agentshore.sidecar.embedded_bridge",
    "agentshore.sidecar.build_id",
)


def _build_probe_script() -> str:
    imports = "\n".join(f"import {mod}" for mod in _SIDECAR_BOOT_MODULES)
    return (
        "import sys\n"
        + imports
        + "\n"
        + 'torch_loaded = "torch" in sys.modules\n'
        + 'torch_modules = sorted(m for m in sys.modules if m == "torch" or m.startswith("torch."))\n'
        + 'print("TORCH_LOADED:" + ("yes" if torch_loaded else "no"))\n'
        + 'print("TORCH_MODULES:" + ",".join(torch_modules))\n'
    )


def test_sidecar_cold_start_does_not_import_torch() -> None:
    """``import agentshore.sidecar.*`` must not transitively load torch."""
    script = _build_probe_script()
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    out = result.stdout

    assert "TORCH_LOADED:no" in out, (
        f"torch was loaded by sidecar cold start. stdout:\n{out}\nstderr:\n{result.stderr}"
    )

    # Cross-check: no torch.* modules present either.
    for line in out.splitlines():
        if line.startswith("TORCH_MODULES:"):
            modules = line[len("TORCH_MODULES:") :]
            assert modules == "", (
                f"torch.* modules unexpectedly present in cold sidecar: {modules!r}"
            )
            break
    else:
        raise AssertionError("probe script did not emit TORCH_MODULES line")
