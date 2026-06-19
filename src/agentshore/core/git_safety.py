"""Git safety helpers for main-repo branch invariant guard (desktop-kqo5).

The main project repository's HEAD must always be on the default branch
(typically ``refs/heads/main``) at the boundary of every play. The commit
SHA under that ref may advance during a play — ``merge_pr`` legitimately
moves it via ``git merge --no-ff origin/<branch>`` followed by ``git push``.
What must not change is the ref pointer itself.

This module exposes pure-Python helpers (no asyncio, no agentshore state) that
the orchestrator wires into ``_dispatch_play`` / ``_process_completion``
boundaries and the session-start sweeper.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentshore import command
from agentshore.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.command import CommandResult

_logger = get_logger(__name__)

#: Max length of the failing-git stderr surfaced on a restore failure. Keeps the
#: ``main_repo_auto_restore_failed`` event (and any reconcile diagnostics) bounded
#: while preserving the operator-actionable tail of git's error message.
_RESTORE_STDERR_MAX = 500


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Outcome of :func:`restore_default_branch`.

    ``ok`` is True once the working tree is back on the default branch with no
    in-progress merge. ``stderr`` carries the last failing git's stderr (truncated
    to :data:`_RESTORE_STDERR_MAX`) when ``ok`` is False, so the caller can thread a
    concrete reason into ``main_repo_auto_restore_failed`` instead of dropping it on
    the floor (#175). It is ``None`` on success or when git produced no stderr.
    """

    ok: bool
    stderr: str | None = None

    def __bool__(self) -> bool:
        return self.ok


# Substring that flags a path which leaked through bash quoting and ended up
# with a literal backslash-space. The canonical example is a project dir whose
# name contains a space (e.g. ``~/Dev/Some Project``) becoming a mangled sibling
# ``Some\ Project`` when a skill template's quoting dropped a level. The check is
# intentionally a substring scan rather than a regex — there is no
# legitimate filesystem path that should contain ``\ `` on macOS or Linux.
PATH_ESCAPE_MARKER = "\\ "

# Fallback default branch name if ``origin/HEAD`` cannot be resolved at
# session start. ``main`` matches GitHub's default for new repos and is the
# most common configuration in the AgentShore fleet.
DEFAULT_BRANCH_FALLBACK = "main"


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    timeout: float = 10.0,
) -> CommandResult:
    """Run ``git`` synchronously in *cwd* and capture text output.

    Returned ``returncode`` is non-zero on failure; callers branch on it
    instead of letting subprocess raise so the guard never crashes the
    orchestrator's main loop. Timeout protects against hung git processes
    (e.g. an interactive credential prompt sneaking in).
    """
    return command.git_sync(*args, cwd=cwd, timeout_seconds=timeout)


def resolve_default_branch(repo_root: Path) -> tuple[str, bool]:
    """Resolve the project's default branch name (e.g. ``"main"``).

    Returns ``(branch, assumed)``. ``assumed`` is True when ``origin/HEAD``
    could not be read and the caller should emit a
    ``default_branch_assumed`` warning so operators can configure
    ``project.target_branch`` explicitly.

    The resolution order matches ``cli/commands/init._detect_default_target_branch``:
    1. ``git symbolic-ref refs/remotes/origin/HEAD`` (the GitHub default).
    2. Fallback to :data:`DEFAULT_BRANCH_FALLBACK` with ``assumed=True``.
    """
    result = _run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], repo_root)
    if result.returncode == 0:
        ref = result.stdout.strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix):
            branch = ref[len(prefix) :].strip()
            if branch:
                return branch, False
    return DEFAULT_BRANCH_FALLBACK, True


def current_head_ref(repo_root: Path) -> str | None:
    """Return the symbolic ref currently checked out, e.g. ``"refs/heads/main"``.

    Returns ``None`` when HEAD is detached (``git symbolic-ref HEAD`` exits
    non-zero in that case) or when the git invocation itself fails. The
    boundary guard treats both cases identically — any normal play should
    leave HEAD on a branch.
    """
    result = _run_git(["symbolic-ref", "HEAD"], repo_root)
    if result.returncode != 0:
        return None
    ref = result.stdout.strip()
    return ref or None


#: Subdir under ``.agentshore/`` where :func:`restore_default_branch`
#: quarantines untracked files that block the default-branch checkout. Mirrors
#: ``trunk_artifacts.QUARANTINE_DIRNAME`` so recovered content lands in one
#: well-known, gitignored place (``.agentshore/`` never re-flags as dirty trunk).
_RESTORE_RECLAIM_DIRNAME = "reclaimed"


def _unique_restore_reclaim_dir(repo_root: Path) -> Path:
    """Pick a non-colliding ``.agentshore/reclaimed/restore[-N]/`` directory.

    Deterministic suffix-bump (no wall-clock / randomness) so a second restore
    in the same session never clobbers an earlier quarantine.
    """
    base = repo_root / ".agentshore" / _RESTORE_RECLAIM_DIRNAME
    n = 0
    while True:
        candidate = base / ("restore" if n == 0 else f"restore-{n}")
        if not candidate.exists():
            return candidate
        n += 1


def _quarantine_untracked_blockers(repo_root: Path) -> list[str]:
    """Move untracked, non-ignored files aside so the default-branch checkout lands.

    ``git checkout <default>`` is refused when untracked working-tree files
    would be overwritten by the target branch, and neither ``merge --abort`` nor
    ``reset --hard`` clears untracked state — so a play that ran in the main
    checkout and left untracked files latches a permanent trunk-dispatch pause
    (the #175 wedge). AgentShore owns this main checkout and the contaminating
    work is recoverable, so we **move** (never delete) the untracked-non-ignored
    set into ``.agentshore/reclaimed/restore[-N]/`` (preserving relative paths)
    and let the checkout proceed. ``--exclude-standard`` keeps gitignored runtime
    state (``.agentshore/``, build output) in place. Best-effort; returns the
    relative paths actually moved and never raises.
    """
    listing = _run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"], repo_root, timeout=30.0
    )
    if listing.returncode != 0 or not listing.stdout:
        return []
    rels = [p for p in listing.stdout.split("\0") if p]
    if not rels:
        return []
    dest_root = _unique_restore_reclaim_dir(repo_root)
    moved: list[str] = []
    for rel in rels:
        src = repo_root / rel
        try:
            if not src.is_file():
                continue
            dst = dest_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
            moved.append(rel)
        except OSError as exc:
            _logger.warning("main_repo_restore_quarantine_failed", path=rel, error=str(exc))
    return moved


def restore_default_branch(repo_root: Path, default_branch: str) -> RestoreResult:
    """Best-effort restore of *repo_root* to a clean ``default_branch`` checkout.

    Returns a :class:`RestoreResult`. ``ok`` is True once the working tree is back
    on ``default_branch`` with no in-progress merge, False only when git still
    refuses after recovery — in which case ``stderr`` carries the last failing
    checkout's stderr (truncated) so the caller can thread a concrete reason into
    ``main_repo_auto_restore_failed`` instead of pausing dispatch on an opaque
    failure (#175). ``RestoreResult`` is truthy on success, so the legacy
    ``if restore_default_branch(...):`` callers keep working.

    Recovery ladder (desktop-kqo5 + #175 wedge fixes). A bare ``git checkout``
    cannot proceed when the main checkout is dirty; AgentShore owns this checkout
    and the contaminating work lives on a feature/PR branch, so each tier clears
    one class of blocker and retries:

    1. **In-progress merge** (``.git/MERGE_HEAD`` present, ``UU`` conflicts) — an
       errant/killed ``merge_pr``: ``git merge --abort``.
    2. **Tracked dirt** (modified/added tracked files, including un-attributable
       trunk modifications): ``git reset --hard``. A hard reset of AgentShore's own
       main checkout is safe — real work lives on feature/PR branches in worktrees —
       so this deterministic backstop clears the tracked-dirt latch the reconcile
       skill's stricter attribution policy refuses to (#175 follow-up).
    3. **Untracked dirt** (the #175 case): untracked files a trunk-scoped play
       left in the main checkout block ``checkout`` ("would be overwritten"), and
       tiers 1–2 don't touch untracked state. :func:`_quarantine_untracked_blockers`
       *moves* them (recoverably) into ``.agentshore/reclaimed/`` so the checkout
       can land. Without this the orchestrator latches a permanent trunk-dispatch
       pause on a fully recoverable state.

    Uses a slightly longer timeout than the symbolic-ref reads because
    checkout/reset may have to walk the index.
    """
    last_stderr: str | None = None

    def _checkout() -> bool:
        nonlocal last_stderr
        result = _run_git(["checkout", default_branch], repo_root, timeout=30.0)
        if result.returncode == 0:
            return True
        stderr = result.stderr.strip()
        last_stderr = stderr[:_RESTORE_STDERR_MAX] if stderr else None
        return False

    if _checkout():
        return RestoreResult(ok=True)

    # Checkout was refused — recover from an in-progress merge / dirty index.
    # ``--abort`` is the merge-specific unwind; ``reset --hard`` then drops the
    # conflicted/dirty *tracked* worktree+index so the checkout can land.
    merge_in_progress = (repo_root / ".git" / "MERGE_HEAD").exists()
    for recovery in (["merge", "--abort"], ["reset", "--hard"]):
        if recovery == ["merge", "--abort"] and not merge_in_progress:
            continue
        _run_git(recovery, repo_root, timeout=30.0)
        if _checkout():
            return RestoreResult(ok=True)

    # Still refused after merge-abort + reset --hard: the blocker is untracked
    # working-tree files the default branch would overwrite (#175). Quarantine
    # them (recoverable) and retry, so a branch-switched HEAD left with untracked
    # work no longer wedges dispatch.
    quarantined = _quarantine_untracked_blockers(repo_root)
    if quarantined:
        _logger.warning(
            "main_repo_restore_quarantined_untracked",
            repo_root=str(repo_root),
            count=len(quarantined),
            paths=quarantined[:20],
        )
        if _checkout():
            return RestoreResult(ok=True)
    if _checkout():
        return RestoreResult(ok=True)
    return RestoreResult(ok=False, stderr=last_stderr)


def path_contains_backslash_space(path: str | Path) -> bool:
    """True if *path* contains a literal backslash-space sequence.

    Catches the desktop-4ugk failure mode where a quoting bug in a skill
    template generated a path like ``/Users/example/Dev/Some\\ Project``
    on disk. There is no legitimate POSIX path that should match.
    """
    return PATH_ESCAPE_MARKER in str(path)


# Canonical set of repo-root paths AgentShore (or its plays / sidecars) owns.
# Single source of truth for BOTH consumers so they can't drift (#594): the
# gitignore writer below ignores them (left untracked they dirty the trunk and
# block merge_pr / reconcile_state on the next run), and
# ``wedge_signals._AGENTSHORE_OWNED_UNTRACKED_PREFIXES`` derives from this to
# filter them out of the dirty-trunk wedge signal. Previously two hand-kept
# literals had drifted — the two ``*_refs.txt`` artifacts were gitignored here
# but still counted dirty by wedge_signals.
AGENTSHORE_OWNED_ROOT_PATHS: tuple[str, ...] = (
    ".agentshore/",
    ".agents/",
    ".beads/",
    "agentshore.yaml",
    "timelapse-runs/",
    "closed_issue_refs.txt",
    "open_bead_refs.txt",
)

_REQUIRED_GITIGNORE_ENTRIES: tuple[str, ...] = AGENTSHORE_OWNED_ROOT_PATHS


def ensure_gitignore_entries(repo_root: Path) -> list[str]:
    """Ensure artifact paths are listed in the project ``.gitignore``.

    Returns the list of entries that were appended (empty if all already
    present).  The function is idempotent and creates ``.gitignore`` if it
    does not exist.
    """
    from agentshore.cli_helpers import _ensure_gitignore_entry

    added: list[str] = []
    for entry in _REQUIRED_GITIGNORE_ENTRIES:
        if _ensure_gitignore_entry(repo_root, entry):
            added.append(entry)
    return added


def untrack_ignored_entries(repo_root: Path) -> list[str]:
    """Untrack required entries that are gitignored but still tracked by git.

    Adding a path to ``.gitignore`` has *no effect* if that path was committed
    before the ignore line existed — git keeps tracking it, so the ignore is a
    silent no-op (observed for ``.beads/`` on a repo that committed it early).
    For each entry in :data:`_REQUIRED_GITIGNORE_ENTRIES` that is currently
    tracked, run ``git rm -r --cached`` to stage its removal from version
    control while leaving the working-tree copy in place (``--cached``).

    Returns the list of entries that were untracked (empty if none were
    tracked). Idempotent — a path already untracked is skipped. The staged
    removals are committed by :func:`commit_gitignore_if_dirty`.
    """
    untracked: list[str] = []
    for entry in _REQUIRED_GITIGNORE_ENTRIES:
        ls = _run_git(["ls-files", "--", entry], repo_root)
        if ls.returncode != 0 or not ls.stdout.strip():
            continue  # entry is not tracked — nothing to untrack
        rm = _run_git(
            ["rm", "-r", "--cached", "--ignore-unmatch", "--", entry],
            repo_root,
        )
        if rm.returncode == 0:
            untracked.append(entry)
    return untracked


def commit_gitignore_if_dirty(repo_root: Path) -> bool:
    """Commit staged ``.gitignore`` edits and untrack removals in one commit.

    Called after :func:`ensure_gitignore_entries` and
    :func:`untrack_ignored_entries` to keep trunk clean so downstream plays
    (especially ``merge_pr``) don't block on a dirty working tree. Stages
    ``.gitignore`` (the ``git rm --cached`` removals are already staged) and
    commits if anything is staged. Returns True if a commit was created.

    Bootstrap runs on a clean working tree (the main-repo branch invariant
    guard ensures HEAD is on the default branch with no in-flight edits), so
    "anything staged" is exactly the gitignore/untrack changes this sweep
    produced.
    """
    add = _run_git(["add", "--", ".gitignore"], repo_root)
    if add.returncode != 0:
        return False
    # returncode 0 == no staged changes; 1 == staged changes present.
    staged = _run_git(["diff", "--cached", "--quiet"], repo_root)
    if staged.returncode == 0:
        return False
    commit = _run_git(
        ["commit", "-m", "chore: ignore and untrack AgentShore artifact paths"],
        repo_root,
        timeout=30.0,
    )
    return commit.returncode == 0


def find_path_escape_siblings(project_root: Path) -> list[Path]:
    """Return sibling directories of *project_root* whose name has backslash-space.

    Scans only the immediate parent (no recursion) — the canonical bug is a
    sibling-of-project escape, not nested. Returns an empty list when the
    parent is unreadable; never raises.
    """
    parent = project_root.parent
    if not parent.exists() or not parent.is_dir():
        return []
    try:
        candidates = list(parent.iterdir())
    except OSError:
        return []
    return [p for p in candidates if path_contains_backslash_space(p.name)]


def check_main_repo_branch_mutated(
    repo_root: Path,
    *,
    pre_ref: str | None,
    default_branch: str,
) -> tuple[bool, str | None, RestoreResult]:
    """Compare ``pre_ref`` to the live HEAD; return mutation flag and detail.

    Returns ``(mutated, post_ref, restore)``. ``mutated`` is True when the
    ref changed (or HEAD is now detached when pre was a ref). ``post_ref``
    is the current symbolic ref or ``None`` for detached. ``restore`` is the
    :class:`RestoreResult` of the auto-restore checkout attempted after a
    mutation (its ``.ok``/``.stderr`` let the caller log a concrete failure
    reason); on no mutation it is a trivially-``ok`` result.

    The caller logs structured events around this — this function stays
    pure so it can be unit-tested without an orchestrator.
    """
    post_ref = current_head_ref(repo_root)
    mutated = pre_ref != post_ref
    if not mutated:
        return False, post_ref, RestoreResult(ok=True)
    restore = restore_default_branch(repo_root, default_branch)
    return True, post_ref, restore


def _resolve_signing_key() -> str:
    """Return the SSH signing key path to use, as a display string.

    Checks ``gpg.ssh.signingKey`` in the global git config first, then probes
    common default key filenames under ``~/.ssh``. Falls back to the generic
    placeholder ``<your-signing-key>`` when nothing is found.
    """
    import pathlib

    # Use the hardened git_sync wrapper (stdin=DEVNULL, CREATE_NO_WINDOW) rather
    # than raw subprocess.run. In the desktop sidecar, the process's stdin is the
    # live Tauri JSON-RPC pipe; git's MSYS2 runtime probes stdin on startup and
    # wedges at 0 CPU forever when it inherits that pipe.
    result = command.git_sync(
        "config", "--global", "--get", "gpg.ssh.signingKey", timeout_seconds=5
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    ssh_dir = pathlib.Path.home() / ".ssh"
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        if (ssh_dir / name).exists():
            return str(ssh_dir / name)

    return "<your-signing-key>"


def ssh_signing_setup_hint() -> str:
    """Platform-appropriate command(s) to load an SSH signing key into the agent.

    The macOS ``--apple-use-keychain`` flag does not exist on Windows or Linux,
    so the user-facing fix text must vary by platform. On Windows the usual
    blocker is that the OpenSSH ``ssh-agent`` service is not running.
    """
    key = _resolve_signing_key()
    if sys.platform == "darwin":
        return f"ssh-add --apple-use-keychain {key}"
    if sys.platform.startswith("win"):
        return (
            f"Start-Service ssh-agent; ssh-add {key}  "
            "(first time only: Set-Service ssh-agent -StartupType Manual)"
        )
    return f"ssh-add {key}"


def ssh_signing_enabled(repo_root: Path) -> bool:
    """True when the repo's effective git config enables SSH commit signing.

    Checks ``commit.gpgsign`` (truthy) and ``gpg.format == ssh`` via the merged
    git config (repo + global + system). The SSH-key pre-flight should only
    fire for setups that actually sign commits — otherwise it cries wolf on the
    majority of repos (and every Windows box) that commit unsigned. Returns
    False on any git error.
    """
    gpgsign = _run_git(["config", "--type=bool", "--get", "commit.gpgsign"], repo_root)
    if gpgsign.returncode != 0 or gpgsign.stdout.strip() != "true":
        return False
    fmt = _run_git(["config", "--get", "gpg.format"], repo_root)
    return fmt.returncode == 0 and fmt.stdout.strip().lower() == "ssh"


def ensure_ssh_signing_key_loaded() -> tuple[bool, str]:
    """Attempt to load the SSH signing key from the macOS Keychain.

    Runs ``ssh-add -l`` to check if any identity is loaded. If not,
    resolves the signing key via ``_resolve_signing_key()`` (git config
    ``gpg.ssh.signingKey`` → common key file probe) and attempts a
    non-interactive ``ssh-add`` with the platform-appropriate flags.

    Returns ``(loaded, detail)`` where *loaded* is True when at least
    one identity is available after the attempt, and *detail* is a
    human-readable status string for logging.

    On Windows, checks for the ssh-agent service and attempts to add
    the default key via ``ssh-add``. Falls back gracefully when the
    agent is unavailable.

    This is idempotent — safe to call every session start.
    """
    import platform
    import shutil

    ssh_add = shutil.which("ssh-add")
    if ssh_add is None:
        return False, "ssh-add not found on PATH"

    def _keys_loaded() -> tuple[bool, str]:
        try:
            result = subprocess.run(
                [ssh_add, "-l"],
                capture_output=True,
                text=True,
                timeout=5,
                # Never inherit the sidecar's stdin (the live Tauri JSON-RPC
                # pipe); a subprocess probing it can wedge the session (#155).
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"ssh-add probe failed: {exc}"
        if result.returncode == 0:
            first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
            return True, first
        return False, result.stderr.strip() or "no identities loaded"

    loaded, detail = _keys_loaded()
    if loaded:
        return True, detail

    # Attempt non-interactive load from the platform keychain / default key.
    import pathlib

    key_str = _resolve_signing_key()
    if key_str == "<your-signing-key>":
        return False, "no identities loaded and no SSH signing key found under ~/.ssh"

    default_key = pathlib.Path(key_str).expanduser()
    if not default_key.exists():
        return False, f"no identities loaded and {default_key} does not exist"

    system = platform.system()
    if system == "Darwin":
        add_cmd = [ssh_add, "--apple-use-keychain", str(default_key)]
    else:
        add_cmd = [ssh_add, str(default_key)]

    try:
        add_result = subprocess.run(
            add_cmd,
            capture_output=True,
            text=True,
            timeout=10,
            # A passphrase-protected key would otherwise prompt on the inherited
            # stdin (the live Tauri JSON-RPC pipe); DEVNULL fails fast instead of
            # contending the pipe (#155).
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ssh-add load attempt failed: {exc}"

    if add_result.returncode != 0:
        stderr = add_result.stderr.strip()
        return False, f"ssh-add load failed: {stderr}"

    # Re-check after loading
    return _keys_loaded()


__all__ = [
    "DEFAULT_BRANCH_FALLBACK",
    "PATH_ESCAPE_MARKER",
    "RestoreResult",
    "check_main_repo_branch_mutated",
    "current_head_ref",
    "commit_gitignore_if_dirty",
    "ensure_gitignore_entries",
    "ensure_ssh_signing_key_loaded",
    "find_path_escape_siblings",
    "path_contains_backslash_space",
    "resolve_default_branch",
    "restore_default_branch",
    "untrack_ignored_entries",
]
