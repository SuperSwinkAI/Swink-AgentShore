"""Skill installation -- copy bundled skill templates into a project."""

from __future__ import annotations

__all__ = ["install_skills", "uninstall_skills"]

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.version import InvalidVersion, Version

from agentshore import __version__

if TYPE_CHECKING:
    from collections.abc import Iterable

# Bundled templates: src/agentshore/skills/templates/<skill-name>/SKILL.md →
# installed into .agents/skills/ in the target project.
_PACKAGE_DIR = Path(__file__).resolve().parent
_BUNDLED_TEMPLATES = _PACKAGE_DIR / "templates"

_VERSION_RE = re.compile(r"^agentshore_version:\s*(.+)$", re.MULTILINE)


def _parse_agentshore_version(text: str) -> str | None:
    """Extract the ``agentshore_version`` value from skill frontmatter.

    Returns ``None`` when the field is absent (indicating a user-modified file
    or a legacy skill that predates version stamping).
    """
    match = _VERSION_RE.search(text)
    if match:
        return match.group(1).strip().strip('"').strip("'")
    return None


def _should_overwrite(existing_text: str, source_text: str) -> bool:
    """Decide whether *source_text* should replace *existing_text*.

    Rules (from the agent-manager design doc):
    - If the installed file has no ``agentshore_version`` field, treat it as
      user-modified and **do not** overwrite.
    - If the source file has no ``agentshore_version`` field, always overwrite
      (the source is canonical even without a stamp).
    - Otherwise, overwrite when the source version is newer, or when the
      version is equal but the stamped template text differs. Equal-version
      replacement keeps editable/dev installs from leaving stale AgentShore-owned
      skills in target projects.
      Versions are compared as packaging-style version strings.
    """
    existing_ver = _parse_agentshore_version(existing_text)
    if existing_ver is None:
        # Field absent or removed -- treat as user-customised.
        return False

    source_ver = _parse_agentshore_version(source_text)
    if source_ver is None:
        # Source has no version stamp; always overwrite.
        return True

    # Compare with PEP 440 semantics so pre-release/dev stamps (e.g. 1.2.0rc1,
    # 1.2.0.dev3) order correctly. An unparseable version on either side falls
    # back to overwriting only when the stamped text actually differs.
    try:
        source = Version(source_ver)
        existing = Version(existing_ver)
    except InvalidVersion:
        return source_text != existing_text
    return source > existing or (source == existing and source_text != existing_text)


def _stamp_version(text: str) -> str:
    """Inject or update ``agentshore_version`` in the YAML frontmatter.

    If the frontmatter already has a ``agentshore_version`` line, replace its
    value.  If the frontmatter exists but lacks the field, insert it after the
    opening ``---``.  If there is no frontmatter, prepend one.
    """
    version = __version__

    if _VERSION_RE.search(text):
        return _VERSION_RE.sub(f"agentshore_version: {version}", text)

    # Has frontmatter delimiters?
    if text.startswith("---"):
        first_newline = text.index("\n")
        before = text[: first_newline + 1]
        after = text[first_newline + 1 :]
        return f"{before}agentshore_version: {version}\n{after}"

    # No frontmatter at all -- prepend one.
    return f"---\nagentshore_version: {version}\n---\n{text}"


def install_skills(
    project_path: Path,
    *,
    force: bool = False,
    only: Iterable[str] | None = None,
) -> list[str]:
    """Copy bundled AgentShore skill files into *project_path*.

    Skills are copied from the single templates directory into
    ``.agents/skills/`` inside the target project.
    Existing skills are only overwritten when their ``agentshore_version``
    frontmatter field is older than the bundled version, unless *force* is
    True (which overwrites regardless of version or user edits).

    When *only* is provided, restrict installation to bundled templates whose
    directory basename appears in that iterable. The default (``None``)
    installs every bundled template, preserving the original behaviour.

    Returns the list of skill names that were installed or updated.
    """
    installed: list[str] = []

    if not _BUNDLED_TEMPLATES.is_dir():
        return installed

    only_set: set[str] | None = set(only) if only is not None else None

    target_root = project_path / ".agents" / "skills"
    target_root.mkdir(parents=True, exist_ok=True)

    for source_entry in sorted(_BUNDLED_TEMPLATES.iterdir()):
        if not source_entry.is_dir():
            continue
        skill_name = source_entry.name
        if only_set is not None and skill_name not in only_set:
            continue
        if not (source_entry / "SKILL.md").is_file():
            continue
        target_dir = target_root / skill_name
        if _install_skill_dir(source_entry, target_dir, force=force):
            installed.append(skill_name)

    return sorted(set(installed))


def _install_skill_dir(
    source_dir: Path,
    target_dir: Path,
    *,
    force: bool = False,
) -> bool:
    """Install a bundled skill directory into *target_dir*.

    Returns True when the skill was installed or updated, False when an
    existing user-customised target was preserved.

    Version-stamp gating applies to SKILL.md only; the rest of the folder
    (``references/``, ``scripts/``, ``assets/``) travels as a unit with
    SKILL.md. When SKILL.md is preserved as user-customised, sibling files
    are left untouched too.
    """
    source_skill_md = source_dir / "SKILL.md"
    source_text = source_skill_md.read_text(encoding="utf-8")
    stamped_source = _stamp_version(source_text)

    target_skill_md = target_dir / "SKILL.md"
    if not force and target_skill_md.is_file():
        existing_text = target_skill_md.read_text(encoding="utf-8")
        if not _should_overwrite(existing_text, stamped_source):
            return False

    target_dir.mkdir(parents=True, exist_ok=True)

    for child in source_dir.iterdir():
        if child.name == "SKILL.md":
            continue
        dest = target_dir / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)

    target_skill_md.write_text(stamped_source, encoding="utf-8")
    return True


def uninstall_skills(project_path: Path) -> list[str]:
    """Remove AgentShore-installed skill files from *project_path*.

    Only removes files that contain a ``agentshore_version`` frontmatter field
    (i.e., files that AgentShore installed rather than user-created skills).

    Returns the list of skill names that were removed.
    """
    removed: list[str] = []

    target_root = project_path / ".agents" / "skills"
    if not target_root.is_dir():
        return removed

    for entry in sorted(target_root.iterdir()):
        skill_name = entry.name

        if entry.is_file() and entry.suffix == ".md":
            text = entry.read_text(encoding="utf-8")
            if _parse_agentshore_version(text) is not None:
                entry.unlink()
                removed.append(skill_name)

        elif entry.is_dir():
            skill_file = entry / "SKILL.md"
            if skill_file.is_file():
                text = skill_file.read_text(encoding="utf-8")
                if _parse_agentshore_version(text) is not None:
                    shutil.rmtree(entry)
                    removed.append(skill_name)

    return sorted(set(removed))
