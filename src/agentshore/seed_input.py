"""Shared seed-input resolution.

Used by both the CLI (``agentshore start --seed``) and the orchestrator
bootstrap config fallback (``intake.seed_paths``). Lives at the package root
(not under ``cli/``) so ``core`` can import it without a CLI dependency.

Raises :class:`SeedInputError` on a missing/unusable seed path; the CLI wrapper
converts that to ``click.BadParameter`` while bootstrap degrades to open-start.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Project state directory name (mirrors ``cli_helpers._PROJECT_DIR``); kept
# local to avoid a CLI import from the package root.
_PROJECT_DIR = ".agentshore"

_SEED_DIR_MAX_TOTAL_BYTES = 512 * 1024
_SEED_SUPPORTED_SUFFIXES: frozenset[str] = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".rst",
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".ini",
        ".cfg",
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".swift",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".sh",
        ".sql",
        ".xml",
        ".html",
        ".css",
    }
)


class SeedInputError(ValueError):
    """The seed path is missing, empty, or otherwise unusable."""


def resolve_seed_input(seed: str, repo_root: Path) -> tuple[Path, str]:
    """Resolve a seed path to a file, expanding directories into a capped bundle.

    Returns ``(path, kind)`` where ``kind`` is ``"file"`` or ``"directory"``.
    Raises :class:`SeedInputError` when the path is missing or has no readable
    UTF-8 content.
    """
    seed_path = Path(seed).expanduser()
    if not seed_path.exists():
        raise SeedInputError(f"Seed path does not exist: {seed}")
    if seed_path.is_file():
        return seed_path, "file"
    if not seed_path.is_dir():
        raise SeedInputError(f"Seed path is not a file or directory: {seed}")

    files = sorted(
        p
        for p in seed_path.rglob("*")
        if p.is_file() and p.suffix.lower() in _SEED_SUPPORTED_SUFFIXES
    )
    if not files:
        raise SeedInputError(f"Seed directory has no supported files: {seed}")

    remaining = _SEED_DIR_MAX_TOTAL_BYTES
    chunks: list[str] = []
    included = 0
    for path in files:
        if remaining <= 0:
            break
        try:
            data = path.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        encoded = text.encode("utf-8")
        if not encoded:
            continue
        take = min(len(encoded), remaining)
        snippet = encoded[:take].decode("utf-8", errors="ignore")
        rel = path.relative_to(seed_path)
        chunks.append(f"\n## {rel}\n\n{snippet}\n")
        included += 1
        remaining -= len(snippet.encode("utf-8"))

    if included == 0:
        raise SeedInputError(f"Seed directory has no readable UTF-8 files: {seed}")

    seed_dir = repo_root / _PROJECT_DIR / "seed_inputs"
    seed_dir.mkdir(parents=True, exist_ok=True)
    raw = str(seed_path.resolve()).encode("utf-8")
    digest = hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:10]
    out_path = seed_dir / f"seed-dir-{digest}.md"
    body = (
        f"# Seed Material Bundle\n\n"
        f"Source directory: {seed_path.resolve()}\n"
        f"Included files: {included}\n"
        f"UTF-8 content cap: {_SEED_DIR_MAX_TOTAL_BYTES} bytes\n" + "".join(chunks)
    )
    out_path.write_text(body, encoding="utf-8")
    return out_path, "directory"
