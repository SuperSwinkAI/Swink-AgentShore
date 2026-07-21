"""Deterministic per-harness model-list discovery via free, local CLI probes.

Codex, Grok, and Antigravity each expose a subcommand that enumerates
currently-selectable models without spending API tokens:

    codex debug models   -> JSON catalog (slug / display_name / visibility / ...)
    grok models            -> plain text, one model per line, default marked
    agy models              -> plain text, one display-name per line
    swink-coding models --json -> JSON array of per-provider/endpoint rows
                                 (reachable + concrete models); the three tier
                                 aliases are always selectable on top of that
                                 (see the function)

(Confirmed against codex-cli 0.141.0, the current grok CLI, and agy; see the
model-catalog spike notes in docs/design/agents/DESIGN.md.)

Claude Code has no such surface: no flag, no subcommand, and no separate
bundled manifest — its model IDs are baked into the compiled binary with no
way to distinguish current from years-deprecated ones. Discovering its
current models needs an actual LLM-backed agent dispatch, which costs real
API spend and needs explicit user opt-in; that path is intentionally NOT in
this module (see docs/design/agents/DESIGN.md "Claude Code model discovery").

Blocking in nature (subprocess.Popen + tree-kill on timeout, matching
agents.auth_probe.probe_cli_auth); async callers should wrap calls in
asyncio.to_thread.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog

from agentshore import subprocess_env

if TYPE_CHECKING:
    from collections.abc import Callable

_logger = structlog.get_logger(__name__)

DiscoveryStatus = Literal["ok", "unavailable", "timeout", "error"]

# A CLI probe is a local, non-mutating metadata read; 15s is ample headroom
# over any observed response and keeps a desktop "Refresh Models" click responsive.
DEFAULT_DISCOVERY_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class DiscoveryResult:
    """Outcome of probing one harness's CLI for its current model list.

    ``status`` mirrors auth_probe's vocabulary shape: ``ok`` (models
    populated), ``unavailable`` (binary not on PATH — not an error, just not
    installed), ``timeout``, or ``error`` (spawned but failed / produced
    unparseable output).
    """

    agent_key: str
    models: tuple[str, ...]
    status: DiscoveryStatus
    detail: str = ""


@dataclass(frozen=True)
class _ProcResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False
    spawn_error: str | None = None


def _run_probe(argv: list[str], *, timeout: float, cwd: str | None = None) -> _ProcResult:
    """Run *argv*, tree-killing on timeout. Never raises.

    Mirrors auth_probe.probe_cli_auth's process handling: Popen (not
    subprocess.run) so a timeout can tree-kill node/python CLI shims —
    subprocess.run's own timeout kill only reaps the direct child and leaves
    the real subtree alive. Shared verbatim by model_discovery_llm.py so the
    safety-critical tree-kill path has exactly one implementation.
    """
    try:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, resolved binary
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=dict(os.environ),
            cwd=cwd,
            creationflags=subprocess_env.no_window_creationflags(),
        )
    except OSError as exc:
        _logger.warning("model_discovery.spawn_failed", argv=argv, error=str(exc))
        return _ProcResult("", "", -1, spawn_error=str(exc)[:200])

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if proc.pid is not None:
            subprocess_env.kill_tree_sync(proc.pid)
        proc.kill()
        proc.communicate()
        return _ProcResult("", "", -1, timed_out=True)
    return _ProcResult(stdout or "", stderr or "", proc.returncode)


def _result_from_proc(
    agent_key: str, result: _ProcResult, *, timeout: float
) -> DiscoveryResult | None:
    """Map a non-ok _ProcResult to a terminal DiscoveryResult, or None on success."""
    if result.timed_out:
        return DiscoveryResult(agent_key, (), "timeout", f"timed out after {timeout:g}s")
    if result.spawn_error:
        return DiscoveryResult(agent_key, (), "error", result.spawn_error)
    if result.returncode != 0:
        detail = f"exit {result.returncode}: {result.stderr.strip()[:200]}"
        return DiscoveryResult(agent_key, (), "error", detail)
    return None


def _parse_codex_models(stdout: str) -> tuple[str, ...]:
    """Parse `codex debug models` JSON: visible slugs only (hidden = internal)."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict):
        return ()
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return ()
    return tuple(
        model["slug"]
        for model in raw_models
        if isinstance(model, dict)
        and isinstance(model.get("slug"), str)
        and model.get("visibility") != "hide"
    )


def _parse_bullet_list(stdout: str) -> tuple[str, ...]:
    """Parse `grok models` output: lines like '* name (default)' / '- name'."""
    models: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] not in "*-":
            continue
        name = stripped[1:].strip().removesuffix("(default)").strip()
        if name:
            models.append(name)
    return tuple(models)


def _parse_plain_lines(stdout: str) -> tuple[str, ...]:
    """Parse `agy models` output: one model display-name per non-empty line."""
    return tuple(line.strip() for line in stdout.splitlines() if line.strip())


def discover_codex_models(
    *, binary: str = "codex", timeout: float = DEFAULT_DISCOVERY_TIMEOUT_S
) -> DiscoveryResult:
    """Probe `codex debug models` (structured JSON, no API key needed)."""
    resolved = shutil.which(binary)
    if resolved is None:
        return DiscoveryResult("codex", (), "unavailable", f"{binary!r} not found on PATH")
    result = _run_probe([resolved, "debug", "models"], timeout=timeout)
    terminal = _result_from_proc("codex", result, timeout=timeout)
    if terminal is not None:
        return terminal
    models = _parse_codex_models(result.stdout)
    if not models:
        detail = "no visible models in `codex debug models` output"
        return DiscoveryResult("codex", (), "error", detail)
    return DiscoveryResult("codex", models, "ok")


def discover_grok_models(
    *, binary: str | None = None, timeout: float = DEFAULT_DISCOVERY_TIMEOUT_S
) -> DiscoveryResult:
    """Probe `grok models` (plain text, default-marked, no API key needed)."""
    from agentshore.agents.cli_grok import default_binary

    resolved_name = binary or default_binary()
    resolved = shutil.which(resolved_name)
    if resolved is None:
        return DiscoveryResult("grok", (), "unavailable", f"{resolved_name!r} not found on PATH")
    result = _run_probe([resolved, "models"], timeout=timeout)
    terminal = _result_from_proc("grok", result, timeout=timeout)
    if terminal is not None:
        return terminal
    models = _parse_bullet_list(result.stdout)
    if not models:
        return DiscoveryResult("grok", (), "error", "no models parsed from `grok models` output")
    return DiscoveryResult("grok", models, "ok")


def discover_antigravity_models(
    *, binary: str = "agy", timeout: float = DEFAULT_DISCOVERY_TIMEOUT_S
) -> DiscoveryResult:
    """Probe `agy models` (plain text, no API key needed)."""
    resolved = shutil.which(binary)
    if resolved is None:
        return DiscoveryResult("antigravity", (), "unavailable", f"{binary!r} not found on PATH")
    result = _run_probe([resolved, "models"], timeout=timeout)
    terminal = _result_from_proc("antigravity", result, timeout=timeout)
    if terminal is not None:
        return terminal
    models = _parse_plain_lines(result.stdout)
    if not models:
        detail = "no models parsed from `agy models` output"
        return DiscoveryResult("antigravity", (), "error", detail)
    return DiscoveryResult("antigravity", models, "ok")


# Always-selectable tier aliases: swink-coding's `--model` accepts these
# regardless of what's reachable, and dispatch can now also target a concrete
# `provider:model` string directly (SuperSwink-Coding#282/#283) — so they lead
# the returned list, with reachable concrete models appended after.
_SWINK_CODING_TIER_ALIASES: tuple[str, ...] = ("small", "medium", "large")


def _parse_swink_coding_catalog(stdout: str) -> tuple[tuple[str, ...], str] | None:
    """Parse `swink-coding models --json`: tier aliases + reachable concrete models.

    Returns ``(models, detail)`` with the tier aliases first, followed by each
    reachable provider's models as deduped ``provider:model`` strings (in
    catalog order); *detail* summarizes any unreachable provider/endpoint
    rows, empty if all providers are reachable. Returns None if *stdout* is
    not a JSON list (unparseable / wrong shape).
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None

    models: list[str] = list(_SWINK_CODING_TIER_ALIASES)
    seen = set(models)
    unreachable: list[str] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider")
        if not isinstance(provider, str):
            continue
        endpoint = row.get("endpoint")
        where = f"{provider}@{endpoint}" if isinstance(endpoint, str) else provider
        if row.get("reachable") is not True:
            unreachable.append(f"{where}: unreachable")
            continue
        raw_models = row.get("models")
        if not isinstance(raw_models, list) or not raw_models:
            continue
        for entry in raw_models:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            model_str = f"{provider}:{name}"
            if model_str not in seen:
                seen.add(model_str)
                models.append(model_str)
    return tuple(models), ", ".join(unreachable)


def discover_swink_coding_models(
    *, binary: str = "swink-coding", timeout: float = DEFAULT_DISCOVERY_TIMEOUT_S
) -> DiscoveryResult:
    """Probe `swink-coding models --json` (live free/local/no-token catalog).

    Returns the three tier aliases (always valid ``--model`` values, resolved
    by swink-coding itself) first, then each reachable provider's concrete
    models as ``provider:model`` strings — those are now dispatchable directly
    via ``--tier-map`` (SuperSwink-Coding#282/#283). A row with
    ``reachable: false`` or an empty/null ``models`` list contributes nothing
    to the model list but is summarized in ``detail``. Status is "ok" as soon
    as the verb parses, even if every endpoint is unreachable — dispatch via
    alias still works, so that's not an error condition; only unparseable
    output or a non-zero exit is "error".
    """
    resolved = shutil.which(binary)
    if resolved is None:
        return DiscoveryResult("swink_coding", (), "unavailable", f"{binary!r} not found on PATH")
    result = _run_probe([resolved, "models", "--json"], timeout=timeout)
    terminal = _result_from_proc("swink_coding", result, timeout=timeout)
    if terminal is not None:
        return terminal
    parsed = _parse_swink_coding_catalog(result.stdout)
    if parsed is None:
        return DiscoveryResult(
            "swink_coding", (), "error", "unparseable `swink-coding models --json` output"
        )
    models, detail = parsed
    return DiscoveryResult("swink_coding", models, "ok", detail)


# Ordered so discover_all's dict preserves a stable, deterministic iteration
# order regardless of dict-construction timing.
_FREE_DISCOVERY_FUNCS: tuple[tuple[str, Callable[..., DiscoveryResult]], ...] = (
    ("codex", discover_codex_models),
    ("grok", discover_grok_models),
    ("antigravity", discover_antigravity_models),
    ("swink_coding", discover_swink_coding_models),
)


def discover_all(*, timeout: float = DEFAULT_DISCOVERY_TIMEOUT_S) -> dict[str, DiscoveryResult]:
    """Probe every free (non-LLM) harness's current model list.

    Claude Code is deliberately excluded — see module docstring; its
    discovery path costs API tokens and requires explicit opt-in, so it is a
    separate call the caller must invoke on its own.

    Runs sequentially (matching auth_probe.probe_configured_cli_auth); each
    probe is individually timeout-bounded, so one hung CLI adds at most
    *timeout* seconds rather than blocking indefinitely. Callers that need
    the probes to run concurrently should fan this out via
    ``asyncio.gather(*(asyncio.to_thread(func, timeout=timeout) for _, func in ...))``
    themselves.
    """
    return {key: func(timeout=timeout) for key, func in _FREE_DISCOVERY_FUNCS}
