from __future__ import annotations

import json
from pathlib import Path


def _capability_permissions() -> list[object]:
    capability_path = (
        Path(__file__).resolve().parents[1]
        / "desktop"
        / "src-tauri"
        / "capabilities"
        / "default.json"
    )
    payload = json.loads(capability_path.read_text(encoding="utf-8"))
    return payload["permissions"]


def test_desktop_capabilities_include_minimum_v1_acl() -> None:
    permissions = _capability_permissions()
    string_permissions = {p for p in permissions if isinstance(p, str)}
    object_permissions = {
        p["identifier"]: p for p in permissions if isinstance(p, dict) and "identifier" in p
    }

    assert "dialog:allow-open" in string_permissions
    assert "dialog:allow-save" in string_permissions
    assert "shell:allow-open" in object_permissions
    assert "shell:allow-execute" in object_permissions
    assert "fs:allow-read" in object_permissions


def test_desktop_capabilities_disallow_extra_plugin_grants() -> None:
    permissions = _capability_permissions()
    identifiers: set[str] = set()
    for permission in permissions:
        if isinstance(permission, str):
            identifiers.add(permission)
            continue
        if isinstance(permission, dict):
            value = permission.get("identifier")
            if isinstance(value, str):
                identifiers.add(value)

    banned_prefixes = ("store:", "updater:", "http:", "notification:")
    for identifier in identifiers:
        assert not identifier.startswith(banned_prefixes), identifier
