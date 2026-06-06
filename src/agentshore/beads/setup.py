"""Beads project setup helpers called by `agentshore init`.

Three steps, in order:
  1. ensure_bd_installed()        — verify `bd` is on PATH
  2. bd_init_project(path)        — run `bd init` if .beads/ is absent
  3. bd_setup_for_agent_types(...)— run `bd hooks install` for git integration

These are synchronous-safe wrappers that delegate to asyncio subprocesses
via the helpers in agentshore.beads. They are intentionally kept separate from
the core beads module so the CLI can import them without pulling in the full
async graph-loading machinery.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import structlog

from agentshore.beads import (
    BdError,
    bd,
    ensure_bd_dir_on_path,
    managed_bd_dir,
    resolve_bd_binary,
)
from agentshore.state import AgentType

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger(__name__)

# Supply-chain + change-control pin for the `bd` (beads) binary. bd's CLI
# semantics directly shape the beads graph — e.g. `bd link`'s default
# dependency type changed across releases and silently inverted epic/task
# linkage (blocking every leaf task → zero implementation work). Pinning the
# binary means such a change can't slip in unannounced. Override with the
# AGENTSHORE_BD_VERSION env var (set it empty to disable the check) only after
# re-verifying the skill-template `bd` calls against the new release.
REQUIRED_BD_VERSION = "1.0.4"

# Public beads release repo. ``provision_bd`` downloads the pinned version's
# platform asset from here when bd is otherwise unavailable.
_BEADS_REPO = "gastownhall/beads"


def _check_bd_version(bd_binary: str) -> None:
    """Assert the resolved bd binary matches the pinned version.

    Raises RuntimeError on mismatch. Honours AGENTSHORE_BD_VERSION as an
    override (empty value disables the check entirely).
    """
    expected = os.environ.get("AGENTSHORE_BD_VERSION", REQUIRED_BD_VERSION).strip()
    if not expected:
        return
    try:
        completed = subprocess.run(
            [bd_binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"could not determine bd version via `{bd_binary} --version`: {exc}"
        ) from exc
    match = re.search(r"\d+\.\d+\.\d+", completed.stdout or completed.stderr or "")
    found = match.group(0) if match else (completed.stdout or "").strip()
    if found != expected:
        raise RuntimeError(
            f"bd version {found!r} does not match AgentShore's pinned version {expected!r}. "
            "bd is pinned because its CLI semantics affect the beads graph (e.g. `bd link`'s "
            "default dependency type changed between releases and silently broke epic/task "
            "linkage). Install bd "
            f"{expected} from https://github.com/gastownhall/beads, or set AGENTSHORE_BD_VERSION "
            "to override after re-verifying the skill-template `bd` calls."
        )


# Map AgentType enum values to the bd hooks actor name.  API-only agents
# have no bd setup target and are omitted.
_BD_ACTOR_NAMES: dict[AgentType, str] = {
    AgentType.CLAUDE_CODE: "claude",
    AgentType.CODEX: "codex",
}


def _beads_release_asset(version: str) -> tuple[str, str] | None:
    """Return ``(asset_filename, archive_kind)`` for this platform.

    ``archive_kind`` is ``"zip"`` (Windows) or ``"tar.gz"`` (macOS/Linux).
    Returns ``None`` on an unsupported platform or CPU architecture.
    """
    arch = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(platform.machine().lower())
    if arch is None:
        return None
    if sys.platform.startswith("win"):
        return f"beads_{version}_windows_{arch}.zip", "zip"
    if sys.platform == "darwin":
        return f"beads_{version}_darwin_{arch}.tar.gz", "tar.gz"
    if sys.platform.startswith("linux"):
        return f"beads_{version}_linux_{arch}.tar.gz", "tar.gz"
    return None


def _expected_sha256(checksums_text: str, asset: str) -> str | None:
    """Pull the SHA-256 for *asset* out of a goreleaser ``checksums.txt``."""
    for line in checksums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == asset:
            return parts[0].lower()
    return None


def _extract_bd(data: bytes, kind: str, bd_name: str, dest: Path) -> None:
    """Extract the *bd_name* member from an in-memory archive into *dest*."""
    import io

    if kind == "zip":
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next((n for n in zf.namelist() if n.rsplit("/", 1)[-1] == bd_name), None)
            if member is None:
                raise RuntimeError(f"{bd_name} not found in archive")
            with zf.open(member) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)
        return

    import tarfile

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        member = next((n for n in tf.getnames() if n.rsplit("/", 1)[-1] == bd_name), None)
        if member is None:
            raise RuntimeError(f"{bd_name} not found in archive")
        extracted = tf.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"could not extract {member} from archive")
        with extracted as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out)


def _download_bd(version: str, asset: str, kind: str) -> str:
    """Download, checksum-verify, and install bd; return the installed path."""
    import hashlib

    import httpx

    base = f"https://github.com/{_BEADS_REPO}/releases/download/v{version}"
    with httpx.Client(follow_redirects=True, timeout=120.0) as client:
        archive = client.get(f"{base}/{asset}")
        archive.raise_for_status()
        checksums = client.get(f"{base}/checksums.txt")
        checksums.raise_for_status()

    expected = _expected_sha256(checksums.text, asset)
    if expected is None:
        raise RuntimeError(f"{asset} is not listed in the release checksums.txt")
    actual = hashlib.sha256(archive.content).hexdigest()
    if actual != expected:
        raise RuntimeError(f"sha256 mismatch for {asset}: expected {expected}, got {actual}")

    bd_name = "bd.exe" if sys.platform.startswith("win") else "bd"
    dest_dir = managed_bd_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / bd_name
    _extract_bd(archive.content, kind, bd_name, dest)
    if not sys.platform.startswith("win"):
        dest.chmod(0o755)
    _logger.info("bd_provisioned", path=str(dest), version=version)
    return str(dest)


def _drain_terminal_input() -> None:
    """Discard any buffered terminal input before an interactive prompt.

    The identity / agent-select wizards that run earlier in ``agentshore init``
    use raw-mode keypress readers (beaupy/questo) and can leave the Enter that
    submitted them queued in the console input buffer. Without draining it,
    ``click.confirm`` below reads that stray newline immediately and resolves to
    its default instead of waiting for the user — so the prompt appears but
    never blocks. Best-effort and never raises (no console / piped stdin ⇒
    nothing to drain).
    """
    try:
        if sys.platform.startswith("win"):
            import msvcrt

            while msvcrt.kbhit():
                msvcrt.getwch()
        else:
            import termios

            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def provision_bd(*, assume_yes: bool = False) -> str | None:
    """Best-effort download of the pinned bd into the managed dir.

    Returns the installed path, or ``None`` when the platform is unsupported,
    the user declines the prompt, or the download/verification fails. Never
    raises — the caller falls back to manual-install instructions.
    """
    version = (
        os.environ.get("AGENTSHORE_BD_VERSION", REQUIRED_BD_VERSION).strip() or REQUIRED_BD_VERSION
    )
    asset_info = _beads_release_asset(version)
    if asset_info is None:
        _logger.warning(
            "bd_provision_unsupported_platform",
            platform=sys.platform,
            machine=platform.machine(),
        )
        return None
    asset, kind = asset_info

    if not assume_yes:
        import click

        # Flush any leftover keystrokes so the prompt actually waits for input.
        _drain_terminal_input()
        if not click.confirm(
            f"  bd {version} is required but not installed. "
            f"Download it from github.com/{_BEADS_REPO} now?",
            default=True,
        ):
            return None

    try:
        return _download_bd(version, asset, kind)
    except Exception as exc:  # best-effort: never block init on a download failure
        _logger.warning("bd_provision_failed", error=str(exc), version=version, asset=asset)
        return None


def ensure_bd_installed() -> None:
    """Ensure `bd` is available and matches the pinned version.

    Resolves bd from AGENTSHORE_BD_BIN / PATH / the managed dir. If it is not
    found anywhere, attempts to provision the pinned release into the managed
    dir (prompting first when interactive, auto-yes when headless). Raises
    RuntimeError with install instructions only if bd is still unavailable, or
    if a resolved bd's version does not match REQUIRED_BD_VERSION. Synchronous
    so it can be called from the Click-based `agentshore init` without a loop.
    """
    bd_binary = resolve_bd_binary()
    if bd_binary is None:
        bd_binary = provision_bd(assume_yes=not sys.stdin.isatty())
    if bd_binary is None:
        raise RuntimeError(
            "The bd binary was not found. Set AGENTSHORE_BD_BIN to a bundled binary or install "
            "bd from https://github.com/gastownhall/beads and re-run agentshore init."
        )
    _check_bd_version(bd_binary)
    # Beads' own installers only hint at PATH; make the canonical dir resolvable
    # for this process (and any agent subprocess that inherits its env).
    ensure_bd_dir_on_path()
    _logger.info("bd_available", path=bd_binary, required_version=REQUIRED_BD_VERSION)


async def bd_init_project(project_path: Path) -> None:
    """Run `bd init` in *project_path* if the beads store does not yet exist.

    Idempotent — if ``.beads/`` already exists the call is a no-op.
    Also writes ``.beads/.gitignore`` containing ``*`` so the local bead
    store is never committed to version control.
    """
    beads_dir = project_path / ".beads"
    if beads_dir.exists():
        _logger.info("bd_init_skipped", reason="already_initialised", path=str(beads_dir))
        return

    _logger.info("bd_init_running", project_path=str(project_path))
    await bd("init", cwd=project_path)
    _logger.info("bd_init_done", path=str(beads_dir))

    # Ensure the store is gitignored.
    gitignore = beads_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")


async def bd_setup_for_agent_types(
    project_path: Path,
    enabled_agent_types: set[AgentType],
) -> list[str]:
    """Install bd git hooks for agent-identity tracking.

    Runs ``bd hooks install`` once for the project (bd v1.0.x has a single
    hooks install step, not a per-agent one). The *enabled_agent_types* set
    is used to log which actors are relevant; only CLI agents are relevant
    for beads integration — API agents have no local git identity.

    Returns the list of bd actor names that are active in this project.
    """
    if not (project_path / ".beads").exists():
        _logger.warning("bd_setup_skipped", reason="no_beads_dir")
        return []

    # Install git hooks (idempotent in bd).
    try:
        await bd("hooks", "install", cwd=project_path)
        _logger.info("bd_hooks_installed", project_path=str(project_path))
    except BdError as exc:
        _logger.warning("bd_hooks_install_failed", error=str(exc))

    actors = [_BD_ACTOR_NAMES[at] for at in enabled_agent_types if at in _BD_ACTOR_NAMES]
    if actors:
        _logger.info("bd_agent_actors_configured", actors=actors)
    return actors


def run_beads_init(
    project_path: Path,
    enabled_agent_types: set[AgentType],
) -> None:
    """Synchronous entry point called from `agentshore init` (Click context).

    Runs the full beads setup sequence:
      1. ensure_bd_installed (synchronous check)
      2. bd_init_project      (async, run in a new event loop)
      3. bd_setup_for_agent_types (async, same event loop)

    Any failure in step 1 propagates to the caller; steps 2–3 log warnings
    rather than aborting init so a failed beads step never blocks the rest
    of `agentshore init`.
    """
    ensure_bd_installed()

    async def _run() -> None:
        await bd_init_project(project_path)
        await bd_setup_for_agent_types(project_path, enabled_agent_types)

    asyncio.run(_run())
