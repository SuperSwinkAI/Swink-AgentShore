"""Single source of truth for the AgentShore version.

`pyproject.toml [project].version` is **canonical**. Every other file that
carries the version — the Tauri config, both Cargo manifests, both
`package.json` files, and the resolved entries in the desktop `Cargo.lock` — is
a *mirror* and must equal it. The build spine calls `check()` early and refuses
to build on drift; `write()` propagates a bump from the canonical file to every
mirror. A pytest guard (`tests/packaging/test_version_consistency.py`) enforces
the same invariant on every test run, so drift fails CI even without a desktop
build.

The `Cargo.lock` entries matter beyond tidiness: the signed Tauri build runs
`cargo build --locked`, which hard-fails ("cannot update the lock file because
--locked was passed") if a bumped `Cargo.toml [package].version` no longer
matches its resolved `[[package]]` version in the lock. Keeping the lock in sync
here is what lets a version bump be followed directly by a build.
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

# Cargo.lock is not a simple one-version-line mirror: it carries a `version`
# field for *every* resolved crate. Only the local workspace members track the
# project version, and each must match canonical or `cargo build --locked` fails.
CARGO_LOCK = "desktop/src-tauri/Cargo.lock"
CARGO_LOCK_PACKAGES: tuple[str, ...] = ("agentshore-desktop", "agentshore-provisioner")

# Targeted, formatting-preserving replacements for `write()`. Anchored at line
# start: in Cargo.toml the bare `version = "..."` is the [package] field, while
# dependency versions are inline (`{ version = "..." }`) or indented, so the
# first line-anchored match is always the package version.
_VERSION_LINE: dict[str, re.Pattern[str]] = {
    "json": re.compile(r'^(?P<pre>\s*"version"\s*:\s*")[^"]*(?P<post>")', re.MULTILINE),
    "toml": re.compile(r'^(?P<pre>version\s*=\s*")[^"]*(?P<post>")', re.MULTILINE),
}


def _cargo_lock_pattern(pkg: str) -> re.Pattern[str]:
    """Match the `version = "..."` line of one local crate's `[[package]]` block.

    Cargo writes each block as a column-0 `name = "<pkg>"` immediately followed
    by `version = "..."`, so anchoring on the name line uniquely targets that
    crate's version among the many `version` lines in the lock.
    """
    return re.compile(
        rf'(?P<pre>^name = "{re.escape(pkg)}"\nversion = ")(?P<ver>[^"]*)(?P<post>")',
        re.MULTILINE,
    )


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


def cargo_lock_versions(root: Path | None = None) -> dict[str, str]:
    """Return ``{crate_name: resolved_version}`` for each local workspace crate.

    Reads via ``tomllib`` (the lock is valid TOML) so detection is robust; the
    formatting-preserving rewrite in ``write()`` uses the regex instead.
    """
    root = root or repo_root()
    parsed = tomllib.loads((root / CARGO_LOCK).read_text(encoding="utf-8"))
    packages = parsed.get("package", []) if isinstance(parsed, dict) else []
    found = {
        p["name"]: p["version"]
        for p in packages
        if isinstance(p, dict) and p.get("name") in CARGO_LOCK_PACKAGES and "version" in p
    }
    missing = [pkg for pkg in CARGO_LOCK_PACKAGES if pkg not in found]
    if missing:
        raise RuntimeError(f"{CARGO_LOCK}: workspace crate(s) not found in lock: {missing}")
    return found


def find_drift(root: Path | None = None) -> list[tuple[str, str]]:
    """Return ``[(mirror_path, its_version)]`` for every mirror != canonical."""
    root = root or repo_root()
    canonical = read_canonical(root)
    drift = [(rel, v) for rel, v in mirror_versions(root).items() if v != canonical]
    drift += [
        (f"{CARGO_LOCK} ({pkg})", v)
        for pkg, v in cargo_lock_versions(root).items()
        if v != canonical
    ]
    return drift


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
    if _write_cargo_lock(root, canonical):
        changed.append(CARGO_LOCK)
    return changed


def _write_cargo_lock(root: Path, canonical: str) -> bool:
    """Rewrite each local crate's resolved version in Cargo.lock. Returns True if changed."""
    path = root / CARGO_LOCK
    text = path.read_text(encoding="utf-8")
    new_text = text
    for pkg in CARGO_LOCK_PACKAGES:
        new_text, count = _cargo_lock_pattern(pkg).subn(
            rf"\g<pre>{canonical}\g<post>", new_text, count=1
        )
        if count != 1:
            raise RuntimeError(
                f"{CARGO_LOCK}: expected exactly one [[package]] version line for "
                f"{pkg!r}, matched {count}"
            )
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


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
