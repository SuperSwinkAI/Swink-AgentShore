"""Structural invariants for the sidecar JSON-RPC server."""

from __future__ import annotations

import ast
from pathlib import Path


def test_stdio_server_has_one_implementation() -> None:
    """The shipped stdio loop must not have an unreachable twin implementation."""
    sidecar_root = Path(__file__).parents[2] / "src" / "agentshore" / "sidecar"
    implementations: list[Path] = []

    for source_path in sidecar_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.AsyncFunctionDef) and node.name == "_serve_async"
            for node in ast.walk(tree)
        ):
            implementations.append(source_path.relative_to(sidecar_root))

    assert implementations == [Path("server.py")]
