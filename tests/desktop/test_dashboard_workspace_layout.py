from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_workspace_layout_invariants() -> None:
    root_package = ROOT / "package.json"
    assert root_package.exists(), "repo root package.json must exist"

    root_data = _read_json(root_package)
    workspaces = root_data.get("workspaces")
    assert isinstance(workspaces, list), "root package.json.workspaces must be a list"
    assert "dashboard" in workspaces, "root workspaces must include dashboard"
    assert "desktop" in workspaces, "root workspaces must include desktop"

    dashboard_data = _read_json(ROOT / "dashboard" / "package.json")
    exports = dashboard_data.get("exports")
    assert isinstance(exports, dict), "dashboard package exports must exist"
    dot_export = exports.get(".") if isinstance(exports, dict) else None
    assert isinstance(dot_export, dict), 'dashboard package exports must include "." entry'

    import_entry = dot_export.get("import") if isinstance(dot_export, dict) else None
    assert isinstance(import_entry, str), 'dashboard exports["."].import must be a string'
    assert import_entry.endswith(".js"), 'dashboard exports["."].import must end with .js'

    types_entry = dot_export.get("types") if isinstance(dot_export, dict) else None
    assert isinstance(types_entry, str), 'dashboard exports["."].types must be a string'
    assert types_entry.endswith(".d.ts"), 'dashboard exports["."].types must end with .d.ts'

    desktop_data = _read_json(ROOT / "desktop" / "package.json")
    deps = desktop_data.get("dependencies")
    assert isinstance(deps, dict), "desktop dependencies must be a mapping"
    assert deps.get("agentshore-dashboard") == "*", (
        'desktop dependency "@agentshore/dashboard" must be "*" for workspace linking'
    )


def test_dashboard_css_does_not_lock_desktop_routes() -> None:
    css = (ROOT / "dashboard" / "src" / "dashboard.css").read_text(encoding="utf-8")

    assert not re.search(
        r"^\s*html,\s*\n\s*body\s*\{[^}]*overflow:\s*hidden",
        css,
        flags=re.M | re.S,
    ), (
        "dashboard.css must not apply `overflow: hidden` to all desktop routes"
    )
    assert re.search(
        r"body\.dashboard-active\s*\{[^}]*overflow:\s*hidden",
        css,
        flags=re.S,
    ), "dashboard viewport locking must be scoped to body.dashboard-active"
