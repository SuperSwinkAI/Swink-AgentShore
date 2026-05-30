"""Seed input resolution for ``agentshore start --seed``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from agentshore.seed_input import SeedInputError, resolve_seed_input

if TYPE_CHECKING:
    from pathlib import Path


def _resolve_seed_input_path(seed: str, repo_root: Path) -> tuple[Path, str]:
    """Resolve --seed to a file path, expanding directories into a capped bundle.

    Thin CLI wrapper over :func:`agentshore.seed_input.resolve_seed_input`;
    converts :class:`SeedInputError` to ``click.BadParameter`` so usage errors
    surface with the ``--seed`` hint.
    """
    try:
        return resolve_seed_input(seed, repo_root)
    except SeedInputError as exc:
        raise click.BadParameter(str(exc), param_hint="--seed") from exc
