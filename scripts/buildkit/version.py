"""Single source of truth for the AgentShore version.

`pyproject.toml [project].version` is **canonical**. Every other file that
carries the version — the Tauri config, both Cargo manifests, and both
`package.json` files — is a *mirror* and must equal it. The build spine calls
`check()` early and refuses to build on drift; `write()` propagates a bump from
the canonical file to every mirror. A pytest guard
(`tests/packaging/test_version_consistency.py`) enforces the same invariant on
every test run, so drift fails CI even without a desktop build.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

# (relative path, kind, dotted key path within the file)
CANONICAL: tuple[str, str, tuple[str, ...]] = ("pyproject.toml", "toml", ("project", "version"))
MIRRORS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("desktop/src-tauri/tauri.conf.json", "json", ("version",)),
    ("desktop/src-tauri/Cargo.toml", "toml", ("package", "version")),
    ("desktop/src-tauri/provisioner/Cargo.toml", "toml", ("package", "version")),
    ("desktop/package.json", "json", ("version",)),
    ("dashboard/package.json", "json", ("version",)),
)

# Targeted, formatting-preserving replacements for `write()`. Anchored at line
# start: in Cargo.toml the bare `version = "..."` is the [package] field, while
# dependency versions are inline (`{ version = "..." }`) or indented, so the
# first line-anchored match is always the package version.
_VERSION_LINE: dict[str, re.Pattern[str]] = {
    "json": re.compile(r'^(?P<pre>\s*"version"\s*:\s*")[^"]*(?P<post>")', re.MULTILINE),
    "toml": re.compile(r'^(?P<pre>version\s*=\s*")[^"]*(?P<post>")', re.MULTILINE),
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_nested(data: object, keys: tuple[str, ...]) -> object:
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            raise KeyError(f"missing key path {'.'.join(keys)!r}")
        data = data[key]
    return data


def _read_version(root: Path, rel: str, kind: str, keys: tuple[str, ...]) -> str:
    text = (root / rel).read_text(encoding="utf-8")
    if kind == "toml":
        parsed: object = tomllib.loads(text)
    elif kind == "json":
        parsed = json.loads(text)
    else:  # pragma: no cover - guarded by the constant tables above
        raise ValueError(f"unknown file kind {kind!r}")
    value = _read_nested(parsed, keys)
    if not isinstance(value, str):
        raise TypeError(f"{rel}: version at {'.'.join(keys)} is not a string: {value!r}")
    return value


def read_canonical(root: Path | None = None) -> str:
    root = root or repo_root()
    rel, kind, keys = CANONICAL
    return _read_version(root, rel, kind, keys)


def mirror_versions(root: Path | None = None) -> dict[str, str]:
    root = root or repo_root()
    return {rel: _read_version(root, rel, kind, keys) for rel, kind, keys in MIRRORS}


def find_drift(root: Path | None = None) -> list[tuple[str, str]]:
    """Return ``[(mirror_path, its_version)]`` for every mirror != canonical."""
    root = root or repo_root()
    canonical = read_canonical(root)
    return [(rel, v) for rel, v in mirror_versions(root).items() if v != canonical]


def write(root: Path | None = None) -> list[str]:
    """Rewrite every mirror's version to match canonical. Returns changed paths."""
    root = root or repo_root()
    canonical = read_canonical(root)
    changed: list[str] = []
    for rel, kind, _keys in MIRRORS:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        new_text, count = _VERSION_LINE[kind].subn(
            rf"\g<pre>{canonical}\g<post>", text, count=1
        )
        if count != 1:
            raise RuntimeError(
                f"{rel}: expected exactly one version line to rewrite, matched {count}"
            )
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            changed.append(rel)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="version",
        description="AgentShore version single-source-of-truth check/sync.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="fail (exit 1) if any mirror drifts from the canonical version (default)",
    )
    group.add_argument(
        "--write",
        action="store_true",
        help="propagate the canonical pyproject.toml version to every mirror",
    )
    args = parser.parse_args(argv)
    root = repo_root()
    canonical = read_canonical(root)

    if args.write:
        changed = write(root)
        if changed:
            print(f"Synced version {canonical} -> {len(changed)} file(s):")
            for rel in changed:
                print(f"  {rel}")
        else:
            print(f"All mirrors already at {canonical}; nothing to do.")
        return 0

    drift = find_drift(root)
    if drift:
        print(
            f"Version drift: canonical (pyproject.toml) is {canonical}, but:",
            file=sys.stderr,
        )
        for rel, value in drift:
            print(f"  {rel}: {value}", file=sys.stderr)
        print(
            "Run `uv run python -m scripts.buildkit version --write` to sync.",
            file=sys.stderr,
        )
        return 1
    print(f"Version OK: all mirrors at {canonical}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
