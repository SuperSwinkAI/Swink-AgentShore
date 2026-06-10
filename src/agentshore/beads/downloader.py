"""Binary download/install logic for the bd (beads) CLI tool.

Separated from setup.py so that the version-check and project-init helpers
remain importable without pulling in network I/O or interactive-prompt code.

The key security invariant is:
  * Headless / non-interactive mode **fails with instructions** by default.
    Auto-downloading and executing a third-party binary without user consent
    is unacceptable in CI, agent, or server contexts.
  * Download is only permitted when consent is present:
      1. The caller passes ``assume_yes=True`` (a consented context such as the
         Windows installer's admin wizard), OR
      2. The opt-in env var ``AGENTSHORE_AUTO_INSTALL_BD=1`` is set, OR
      3. The terminal is interactive (``sys.stdin.isatty()`` is True) AND the
         user explicitly confirms the prompt.

When consent is present the pinned release asset is fetched from GitHub,
checksum-verified against the release ``checksums.txt``, and extracted into
``dest_dir`` (the caller-supplied managed location). When consent is absent the
function raises ``RuntimeError`` with manual-install instructions and never
touches the network.
"""

from __future__ import annotations

import os
import platform
import shutil
import ssl
import sys
from pathlib import Path

import structlog

_logger = structlog.get_logger(__name__)

# Opt-in env var for headless/CI auto-download. Set to "1" to allow
# non-interactive bd binary download. Must be explicitly set — the default
# is conservative (fail with instructions).
_AUTO_INSTALL_ENV_VAR = "AGENTSHORE_AUTO_INSTALL_BD"

# Public beads release repo. ``provision_bd`` downloads the pinned version's
# platform asset from here when bd is otherwise unavailable.
_BEADS_REPO = "gastownhall/beads"

_INSTALL_INSTRUCTIONS = (
    "The bd binary was not found. To resolve:\n"
    "  1. Install bd {version} from https://github.com/gastownhall/beads\n"
    "  2. Ensure `bd` is on PATH, or set AGENTSHORE_BD_BIN to the binary path.\n"
    "  3. Re-run `agentshore init`.\n"
    "\n"
    "For non-interactive / CI environments that need automatic install, "
    "set {env_var}=1 to opt in explicitly."
)


def _auto_install_opted_in() -> bool:
    """Return True when the user has explicitly opted in to non-interactive install."""
    return os.environ.get(_AUTO_INSTALL_ENV_VAR, "").strip() == "1"


def provision_bd(
    required_version: str,
    *,
    assume_yes: bool = False,
    dest_dir: Path | None = None,
) -> str | None:
    """Ensure the bd binary is available, downloading it only when permitted.

    Decision tree:
    1. If bd is already on PATH (or AGENTSHORE_BD_BIN), return its path — the
       version check is the caller's responsibility (see ``_check_bd_version``).
    2. Otherwise decide whether consent to download is present:
       * ``assume_yes=True`` (a consented caller, e.g. the installer), OR
       * ``AGENTSHORE_AUTO_INSTALL_BD=1`` (explicit non-interactive opt-in), OR
       * an interactive TTY where the user confirms the prompt.
    3. With consent, download + checksum-verify + extract the pinned release
       asset into ``dest_dir`` (or a per-user default) and return the installed
       path. Returns ``None`` if the platform is unsupported or the download
       fails — callers fall back to manual-install instructions.
    4. Without consent, raise ``RuntimeError`` with human-readable instructions
       and never touch the network (the headless-fail invariant).

    Parameters
    ----------
    required_version:
        The pinned bd version string (e.g. ``"1.0.4"``). Used both to build the
        release asset URL and in the error message so operators know which
        release to fetch. Honours the ``AGENTSHORE_BD_VERSION`` override.
    assume_yes:
        Skip the interactive prompt and proceed with the download. Set by
        consented callers such as the Windows installer provisioner.
    dest_dir:
        Directory to install bd into. The installer passes the machine-managed
        bin dir; when omitted a per-user default is used.
    """
    from agentshore.beads import resolve_bd_binary

    bd_binary = resolve_bd_binary()
    if bd_binary is not None:
        # Already installed — nothing to provision.
        return bd_binary

    version = os.environ.get("AGENTSHORE_BD_VERSION", required_version).strip() or required_version

    if assume_yes or _auto_install_opted_in():
        _logger.info(
            "bd_auto_install_consented",
            via="assume_yes" if assume_yes else _AUTO_INSTALL_ENV_VAR,
            required_version=version,
        )
        return _download(version, dest_dir)

    if sys.stdin.isatty():
        # Interactive terminal: prompt the user.
        _logger.info("bd_not_found_prompting_user", required_version=version)
        try:
            answer = (
                input(
                    f"\nbd {version} was not found. "
                    "Allow AgentShore to download and install it? [y/N] "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer in ("y", "yes"):
            _logger.info("bd_install_user_confirmed", required_version=version)
            return _download(version, dest_dir)
        _logger.info("bd_install_user_declined", required_version=version)
        _raise_install_instructions(version)

    # Non-interactive and no opt-in: fail conservatively with instructions.
    _logger.warning(
        "bd_not_found_headless_no_opt_in",
        required_version=version,
        hint=f"Set {_AUTO_INSTALL_ENV_VAR}=1 to enable non-interactive install",
    )
    _raise_install_instructions(version)
    return None  # unreachable — _raise_install_instructions always raises


def _default_install_dir() -> Path:
    """Per-user managed bin dir used when the caller does not pass ``dest_dir``."""
    import platformdirs

    return Path(platformdirs.user_data_dir("agentshore", "agentshore")) / "bin"


def _download(version: str, dest_dir: Path | None) -> str | None:
    """Best-effort download of the pinned bd into *dest_dir*.

    Returns the installed path, or ``None`` when the platform is unsupported or
    the download/verification fails. Never raises — the caller falls back to
    manual-install instructions.
    """
    asset_info = _beads_release_asset(version)
    if asset_info is None:
        _logger.warning(
            "bd_provision_unsupported_platform",
            platform=sys.platform,
            machine=platform.machine(),
        )
        return None
    asset, kind = asset_info
    try:
        return _download_bd(version, asset, kind, dest_dir=dest_dir or _default_install_dir())
    except Exception as exc:  # best-effort: never crash the caller on a download failure
        _logger.warning("bd_provision_failed", error=str(exc), version=version, asset=asset)
        return None


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


def _httpx_verify_config() -> bool | ssl.SSLContext:
    """Return TLS verification config for release downloads.

    httpx defaults to certifi, which does not include enterprise roots installed
    in the Windows certificate store. The Windows installer already uses
    ``uv --native-tls`` for the same reason; use Python's native Windows trust
    loading for the bd release download too.
    """
    if sys.platform.startswith("win"):
        return ssl.create_default_context()
    return True


def _download_bd(version: str, asset: str, kind: str, *, dest_dir: Path) -> str:
    """Download, checksum-verify, and install bd; return the installed path."""
    import hashlib

    import httpx

    base = f"https://github.com/{_BEADS_REPO}/releases/download/v{version}"
    with httpx.Client(
        follow_redirects=True, timeout=120.0, verify=_httpx_verify_config()
    ) as client:
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
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / bd_name
    _extract_bd(archive.content, kind, bd_name, dest)
    if not sys.platform.startswith("win"):
        dest.chmod(0o755)
    _logger.info("bd_provisioned", path=str(dest), version=version)
    return str(dest)


def _raise_install_instructions(required_version: str) -> None:
    """Raise RuntimeError with human-readable bd install instructions."""
    raise RuntimeError(
        _INSTALL_INSTRUCTIONS.format(
            version=required_version,
            env_var=_AUTO_INSTALL_ENV_VAR,
        )
    )
