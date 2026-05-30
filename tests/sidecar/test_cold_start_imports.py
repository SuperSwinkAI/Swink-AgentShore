"""Cold-start import guards for desktop sidecar startup paths."""

from __future__ import annotations

import json
import subprocess
import sys


def _imports_torch(module: str) -> bool:
    code = (
        "import importlib, json, sys;"
        f"importlib.import_module({module!r});"
        "print(json.dumps({'torch_loaded': 'torch' in sys.modules}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout.strip())
    return bool(payload["torch_loaded"])


def test_sidecar_server_import_is_torch_free() -> None:
    assert _imports_torch("agentshore.sidecar.server") is False


def test_core_import_is_torch_free_until_orchestrator_start() -> None:
    assert _imports_torch("agentshore.core") is False
