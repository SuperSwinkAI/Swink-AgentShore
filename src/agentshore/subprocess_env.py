"""Windows-hardened subprocess policy for git/gh.

Single source of truth for *how* AgentShore spawns external tools so every call
site inherits the same Windows-correct behavior:

* **Absolute-path tool resolution** (``resolve_tool``) — env override →
  ``shutil.which`` (PATHEXT-aware, resolves ``.cmd``/``.exe`` shims) → explicit
  canonical Win11 install locations. A bare ``"git"``/``"gh"`` name is never
  handed to ``create_subprocess_exec`` (which does not consult PATHEXT and would
  raise ``WinError 2`` on a ``.cmd`` shim). A missing tool returns ``None`` so
  callers degrade with an actionable message instead of an opaque spawn error.
  ``bd`` resolution is NOT here — use ``agentshore.beads.resolve_bd_binary``
  which is the single owner of that logic.
* **Non-interactive credential environment** (``hardened_env`` +
  ``git_global_args``) — the dominant fresh-Windows hang vector is a headless
  ``CREATE_NO_WINDOW`` git/gh popping a Git-Credential-Manager / askpass dialog
  that can never be answered. We disable every interactive prompt path so a
  missing credential fails *fast* instead of hanging to the timeout.
* **CREATE_NO_WINDOW + new process group** (``no_window_creationflags``) — no
  console flashes, no AV window-hooking latency, and the whole child tree is
  killable.
* **schannel TLS on Windows** — git uses the Windows certificate store (where
  an Avast/Defender HTTPS-scanning MITM root is installed) instead of its
  bundled OpenSSL CA bundle, which rejects the AV cert. Overridable via
  ``AGENTSHORE_GIT_SSL_BACKEND`` (set empty to disable).
* **Windows-aware timeouts** (``timeout_for``) — a per-op-class table with a
  win32 multiplier to absorb AV first-touch scanning latency.

This module is intentionally dependency-light (stdlib only) so the low-level
``agentshore.command`` runner can import it without a cycle.
"""

from __future__ import annotations

import base64
import contextlib
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "no_window_creationflags",
    "resolve_tool",
    "reset_caches",
    "hardened_env",
    "git_global_args",
    "git_auth_config_overlay",
    "timeout_for",
    "kill_tree_sync",
    "is_interactive",
    "GIT_SSL_BACKEND_ENV",
    "TOOL_TIMEOUT_SCALE_ENV",
    "GIT_EDITOR_ENV",
    "GIT_SEQUENCE_EDITOR_ENV",
    "NONINTERACTIVE_ENV",
]

NONINTERACTIVE_ENV = "AGENTSHORE_NONINTERACTIVE"


def is_interactive() -> bool:
    """Return whether the process may safely run interactive prompts.

    Single source of truth for the "should I prompt?" decision shared by the
    agent-setup wizard, the identity wizard, and ``agentshore init``. A prompt
    is allowed only when ``AGENTSHORE_NONINTERACTIVE`` is unset *and* stdin is a
    real TTY. Honouring the env var matters even on a TTY: scripted/CI runs and
    the desktop sidecar set it to force every wizard to skip cleanly instead of
    blocking on input that will never arrive.
    """
    if os.environ.get(NONINTERACTIVE_ENV):
        return False
    return sys.stdin.isatty()


GIT_SSL_BACKEND_ENV = "AGENTSHORE_GIT_SSL_BACKEND"
TOOL_TIMEOUT_SCALE_ENV = "AGENTSHORE_TOOL_TIMEOUT_SCALE"
GIT_EDITOR_ENV = "GIT_EDITOR"
GIT_SEQUENCE_EDITOR_ENV = "GIT_SEQUENCE_EDITOR"

# Env override knobs so support/enterprise can pin a tool path without a rebuild.
# bd is not here — use agentshore.beads.resolve_bd_binary (single owner).
_TOOL_ENV_OVERRIDE: dict[str, str] = {
    "git": "AGENTSHORE_GIT_BIN",
    "gh": "AGENTSHORE_GH_BIN",
}


def no_window_creationflags() -> int:
    """Return ``CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`` on Windows, else 0.

    ``CREATE_NO_WINDOW`` suppresses the console-window flash and avoids the AV
    window-hooking latency every git/gh spawn otherwise incurs.
    ``CREATE_NEW_PROCESS_GROUP`` roots the child in its own group so the whole
    tree (git → credential helper / ssh) is killable via ``taskkill /T``.
    """
    if sys.platform == "win32":
        import subprocess

        # ``getattr`` with a 0 default: the flags are Windows-only attributes, so
        # tests that fake ``sys.platform == "win32"`` on a POSIX host (where the
        # attributes are absent) exercise this branch without an AttributeError.
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    return 0


def _canonical_windows_paths(name: str) -> Iterable[Path]:
    """Yield well-known Win11 install locations for *name* (git/gh).

    Probed only after ``shutil.which`` misses, so Store/portable/winget installs
    and a PATH that the parent failed to inherit still resolve.

    ``bd`` is intentionally excluded — ``agentshore.beads.resolve_bd_binary``
    is the single owner of bd resolution.
    """
    exe = f"{name}.exe"

    def env_path(var: str) -> Path | None:
        value = os.environ.get(var)
        return Path(value) if value else None

    candidates: list[Path | None] = []
    program_files = env_path("ProgramFiles") or Path(r"C:\Program Files")
    program_files_x86 = env_path("ProgramFiles(x86)") or Path(r"C:\Program Files (x86)")
    program_w6432 = env_path("ProgramW6432")
    local_appdata = env_path("LOCALAPPDATA")
    programdata = env_path("PROGRAMDATA") or Path(r"C:\ProgramData")

    if name == "git":
        for base in (program_files, program_w6432):
            if base is not None:
                candidates.append(base / "Git" / "cmd" / exe)
                candidates.append(base / "Git" / "bin" / exe)
    elif name == "gh":
        candidates.append(program_files / "GitHub CLI" / exe)
        candidates.append(program_files_x86 / "GitHub CLI" / exe)
        if local_appdata is not None:
            candidates.append(local_appdata / "Programs" / "GitHub CLI" / exe)

    # Common to all: winget shim links and the AgentShore machine-managed bin.
    if local_appdata is not None:
        candidates.append(local_appdata / "Microsoft" / "WinGet" / "Links" / exe)
    candidates.append(programdata / "AgentShore" / "bin" / exe)

    for candidate in candidates:
        if candidate is not None:
            yield candidate


# Positive-hit cache only: a tool installed *after* launch must still be found,
# so a miss is re-probed every call (cheap) while a hit is memoized for the
# process lifetime to avoid re-scanning PATH (which AV re-inspects each time).
_resolved_cache: dict[str, str] = {}


def resolve_tool(name: str) -> str | None:
    """Resolve *name* to an absolute executable path, or ``None`` if absent.

    Order: ``AGENTSHORE_{GIT,GH}_BIN`` env override → ``shutil.which`` →
    explicit canonical Windows locations. Never returns a bare name.

    For ``bd`` use ``agentshore.beads.resolve_bd_binary`` instead — it is the
    single source of truth for bd resolution.
    """
    cached = _resolved_cache.get(name)
    if cached is not None:
        return cached

    override_var = _TOOL_ENV_OVERRIDE.get(name)
    if override_var is not None:
        override = os.environ.get(override_var)
        if override and Path(override).is_file():
            _resolved_cache[name] = override
            return override

    found = shutil.which(name)
    if found is None and sys.platform == "win32":
        for candidate in _canonical_windows_paths(name):
            if candidate.is_file():
                found = str(candidate)
                break

    if found is not None:
        _resolved_cache[name] = found
    return found


def reset_caches() -> None:
    """Clear the resolved-tool cache (test hook / post-install re-probe)."""
    _resolved_cache.clear()


# Per-op-class base timeouts (seconds). Network ops are generous; local probes
# are tight but Windows-scaled at call time.
_BASE_TIMEOUTS: dict[str, float] = {
    "git.read": 15.0,
    "git.network": 120.0,
    "git.mutate": 30.0,
    "gh": 20.0,
    "gh.network": 30.0,
    "keyring": 15.0,
    "identity_check": 25.0,
    "bd": 120.0,
}
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_WIN_SCALE = 1.5


def timeout_for(op_class: str) -> float:
    """Return the timeout for *op_class*, scaled up on Windows for AV latency."""
    base = _BASE_TIMEOUTS.get(op_class, _DEFAULT_TIMEOUT)
    if sys.platform == "win32":
        scale = _DEFAULT_WIN_SCALE
        raw = os.environ.get(TOOL_TIMEOUT_SCALE_ENV)
        if raw:
            with contextlib.suppress(ValueError):
                scale = max(1.0, float(raw))
        base *= scale
    return base


def git_global_args() -> list[str]:
    """``-c`` flags to prepend before any git subcommand.

    Neutralizes every credential-prompt path for the single invocation without
    mutating the user's git config, and (on Windows) selects the schannel TLS
    backend so the Windows cert store — where AV HTTPS-scan roots live — is
    trusted. Disable schannel by setting ``AGENTSHORE_GIT_SSL_BACKEND`` empty.
    """
    args = [
        "-c",
        "credential.helper=",
        "-c",
        "credential.interactive=never",
        "-c",
        "core.askpass=",
    ]
    if sys.platform == "win32":
        backend = os.environ.get(GIT_SSL_BACKEND_ENV, "schannel")
        if backend:
            args += ["-c", f"http.sslBackend={backend}"]
    return args


#: Default GitHub host. Most installs are github.com; GHE installs override via
#: the ``host`` argument (derived from the repo's origin URL by the caller).
GITHUB_HOST = "github.com"

#: GitHub's required username when authenticating with a token over HTTPS.
_GIT_TOKEN_USERNAME = "x-access-token"


def git_auth_config_overlay(token: str, *, host: str = GITHUB_HOST) -> dict[str, str]:
    """Env entries that make non-interactive git authenticate to *host* as *token*.

    Returns ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n``
    env vars that inject, for this process and any git it spawns:

    * ``http.https://<host>/.extraheader = Authorization: Basic <b64>`` — the
      token as HTTP Basic auth (the exact mechanism ``actions/checkout`` uses),
      scoped to ``host`` only so the token never leaks to another remote; and
    * ``credential.helper=`` + ``credential.interactive=never`` — so a bad/expired
      token (401) fails *fast* through the disabled-prompt path instead of
      falling back to an interactive credential helper and hanging.

    Delivered purely via the environment — no on-disk askpass/helper script — so
    it works uniformly for AgentShore's own :func:`agentshore.command.git` calls
    *and* for a CLI agent subprocess running ``git push`` itself. Each process
    carries its *own* identity's token, so the correct identity authenticates
    per process; this is the multi-identity-safe credential path.

    *token* is the GitHub identity token (PAT / ``gh`` OAuth / keychain). It is
    paired with the literal ``x-access-token`` username GitHub expects and lands
    only in ``GIT_CONFIG_VALUE_0`` — an env *value*, never an argv or a log field
    (call sites log env keys only, never values).

    Note: assumes the caller has not pre-populated ``GIT_CONFIG_COUNT`` in the
    base environment (AgentShore never does); these entries replace, not append.
    """
    basic = base64.b64encode(f"{_GIT_TOKEN_USERNAME}:{token}".encode()).decode("ascii")
    entries: tuple[tuple[str, str], ...] = (
        ("credential.helper", ""),
        ("credential.interactive", "never"),
        (f"http.https://{host}/.extraheader", f"Authorization: Basic {basic}"),
    )
    overlay: dict[str, str] = {"GIT_CONFIG_COUNT": str(len(entries))}
    for index, (key, value) in enumerate(entries):
        overlay[f"GIT_CONFIG_KEY_{index}"] = key
        overlay[f"GIT_CONFIG_VALUE_{index}"] = value
    return overlay


def hardened_env(
    overlay: Mapping[str, str] | None = None,
    *,
    for_git: bool = False,
    for_gh: bool = False,
) -> dict[str, str]:
    """Return ``os.environ`` plus a fully non-interactive git/gh environment.

    *overlay* (per-identity ``GH_TOKEN``/``GH_CONFIG_DIR``/``GIT_SSH_COMMAND``)
    is applied last so caller values win. ``None`` overlay values are dropped.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if for_git:
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
        env["SSH_ASKPASS_REQUIRE"] = "never"
        env["GCM_INTERACTIVE"] = "Never"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        # The git *editor* is a separate interactive surface from the
        # credential prompt. Without these, a rebase-internal ``git commit -e``
        # falls back to ``core.editor -> EDITOR -> vi`` (MSYS2 vim on Windows),
        # which opens with no usable console on a detached subprocess and hangs
        # at 0 CPU forever, pinning the worktree (#168). ``true`` is the no-op
        # editor: git treats it as "succeed immediately without editing".
        # ``GIT_EDITOR`` propagates from a parent ``git rebase`` into its
        # internal ``git commit``. (Empty string is wrong here — git reads it
        # as "fall back to the next editor".)
        env[GIT_EDITOR_ENV] = "true"
        env[GIT_SEQUENCE_EDITOR_ENV] = "true"
    if for_gh:
        env["GH_PROMPT_DISABLED"] = "1"
        env["GH_NO_UPDATE_NOTIFIER"] = "1"
        env["GH_PAGER"] = "cat"
        env["CLICOLOR"] = "0"
    if overlay:
        for key, value in overlay.items():
            if value is not None:
                env[key] = value
    return env


def kill_tree_sync(pid: int) -> None:
    """Best-effort kill of the process tree rooted at *pid* (sync callers).

    On Windows ``taskkill /T /F`` walks the tree; elsewhere SIGKILL the group
    then the pid. All failures are swallowed — the caller is in a timeout/cleanup
    path and an already-dead process is success.
    """
    if sys.platform == "win32":
        import subprocess

        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603, S607 — fixed system tool, no shell
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=no_window_creationflags(),
                timeout=10,
                check=False,
            )
        return
    # POSIX: kill only the target pid — never killpg, which could hit the
    # sidecar's own process group (children are not session leaders here). This
    # matches the pre-existing proc.kill() behavior, keeping macOS/Linux
    # unchanged; Windows gets the real tree kill above via taskkill /T.
    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(pid, 9)
