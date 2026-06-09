"""Grok CLI command-shape helpers."""

from __future__ import annotations

import shutil

_GROK_CLI_MODEL_ALIASES: dict[str, str] = {
    "grok-build-0.1": "grok-build",
    "grok-code-fast-1": "grok-build",
    "grok-code-fast": "grok-build",
    "grok-code-fast-1-0825": "grok-build",
}


def default_binary() -> str:
    """Prefer ``grok`` but support hosts that only have the ``grok-build`` alias."""
    if shutil.which("grok") is not None:
        return "grok"
    if shutil.which("grok-build") is not None:
        return "grok-build"
    return "grok"


def cli_model(model: str) -> str:
    """Return the model id accepted by the installed Grok CLI."""
    return _GROK_CLI_MODEL_ALIASES.get(model, model)


def build_argv(
    *,
    prompt: str,
    binary: str | None,
    model: str | None,
    reasoning_effort: str | None,
    extra_flags: tuple[str, ...],
    project_dir: str | None,
    prompt_on_stdin: bool,
) -> list[str]:
    """Return argv for one non-interactive Grok CLI invocation."""
    resolved_binary = binary or default_binary()
    resolved_model = cli_model(model) if model else None
    args = [
        resolved_binary,
        "--no-auto-update",
        "--no-subagents",
        "--verbatim",
    ]
    if project_dir:
        args += ["--cwd", project_dir]
    args += ["--output-format", "streaming-json"]
    if resolved_model:
        args += ["-m", resolved_model]
    if reasoning_effort:
        args += ["--reasoning-effort", reasoning_effort]
    args.extend(extra_flags)
    args += ["-p", "" if prompt_on_stdin else prompt]
    return args
