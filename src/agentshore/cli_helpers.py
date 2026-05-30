"""CLI helper functions and constants extracted from agentshore.cli."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
from pathlib import Path

from agentshore.agents.model_tiers import DEFAULT_MODEL_TIER, default_model_tiers_for
from agentshore.environment import resolve_executable
from agentshore.errors import OrchestratorError
from agentshore.state import AgentType

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BUDGET: float = 200.0
_PROJECT_DIR = ".agentshore"
_AGENT_PRICING_LINES: dict[str, tuple[str, ...]] = {
    "claude_code": (
        "    max_context: 200000",
        "    cost_per_1k_input: 0.003",
        "    cost_per_1k_cached_input: 0.0003",
        "    cost_per_1k_cache_write_input: 0.00375",
        "    cost_per_1k_output: 0.015",
    ),
    "codex": (
        "    max_context: 400000",
        "    cost_per_1k_input: 0.00175",
        "    cost_per_1k_cached_input: 0.000175",
        "    cost_per_1k_output: 0.014",
    ),
    "gemini": (
        "    max_context: 1000000",
        "    cost_per_1k_input: 0.0005",
        "    cost_per_1k_output: 0.003",
    ),
}


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    """Walk up from *start* until a ``.git`` directory is found."""
    current = start.resolve()
    while True:
        if (current / ".git").is_dir():
            return current
        parent = current.parent
        if parent == current:
            raise OrchestratorError(
                "No git repository found.  Run `agentshore start` from inside a git repo.",
                recoverable=False,
            )
        current = parent


def _detect_gh_remote(cwd: Path | None = None) -> dict[str, str]:
    """Return ``{"url": ..., "nameWithOwner": ...}`` from `gh repo view`."""
    gh_path = resolve_executable("gh")
    if gh_path is None:
        raise OrchestratorError(
            "`gh` CLI not found on PATH.  Install the GitHub CLI.",
            recoverable=False,
        )
    try:
        result = subprocess.run(  # nosec B603
            [gh_path, "repo", "view", "--json", "url,nameWithOwner"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        parsed = json.loads(result.stdout)
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Unexpected gh response: {result.stdout!r}")
        return {str(k): str(v) for k, v in parsed.items()}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise OrchestratorError(
            f"Failed to detect GitHub remote: {exc}",
            recoverable=False,
        ) from exc


def _render_or_merge_agentshore_yaml(
    path: Path,
    *,
    name_with_owner: str,
    agents: list[str],
    budget: float | None,
    strict: bool,
) -> bool:
    """Write ``agentshore.yaml`` non-destructively.

    - Path missing → write a fresh template (current behavior).
    - Path exists → merge: replace ONLY the ``agents:`` skeleton with the
      newly-rendered template; every other top-level key (``budget``,
      ``intake``, ``scope``, ``agent_preferences``, ``identities``, plus
      any user-added keys) is preserved verbatim. Comments + key order on
      preserved keys also survive (ruamel.yaml round-trip).

    Returns True if the file was written/modified, False if untouched.
    """
    fresh_text = _generate_default_config(name_with_owner, agents, budget, strict)

    if not path.exists():
        path.write_text(fresh_text)
        return True

    # ruamel.yaml preserves comments + key order. PyYAML cannot.
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True

    existing = rt.load(path.read_text()) or {}
    fresh = rt.load(fresh_text) or {}

    fresh_agents = fresh.get("agents")
    if fresh_agents is None:
        return False
    existing["agents"] = fresh_agents

    import io

    buf = io.StringIO()
    rt.dump(existing, buf)
    path.write_text(buf.getvalue())
    return True


def _detect_agents() -> list[str]:
    """Return names of coding-agent CLIs present on PATH."""
    from agentshore.environment import detect_agent_binaries

    return list(detect_agent_binaries())


def _detect_api_keys() -> dict[str, bool]:
    """Return a map of recognised API-key env vars to their presence."""
    names = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    return {n: True for n in names if os.environ.get(n)}


def _ensure_gitignore_entry(project_path: Path, entry: str = ".agentshore/") -> bool:
    """Ensure *entry* is present in the project's ``.gitignore``.

    Creates ``.gitignore`` if missing. Idempotent — treats ``.agentshore``,
    ``.agentshore/``, ``/.agentshore``, and ``/.agentshore/`` as equivalent. Comment
    lines do not count as a match. Returns True if the file was created or
    modified, False if the entry was already present.
    """
    gitignore = project_path / ".gitignore"
    target = entry.strip().strip("/")

    if not gitignore.exists():
        gitignore.write_text(entry + "\n")
        return True

    existing = gitignore.read_text()
    for line in existing.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.strip("/") == target:
            return False

    separator = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(existing + separator + entry + "\n")
    return True


def _generate_default_config(
    name_with_owner: str,
    agents: list[str],
    budget: float | None,
    strict: bool,
) -> str:
    """Render a minimal ``agentshore.yaml`` as a YAML string."""
    budget_enabled = budget is not None and budget > 0
    budget_total = budget if budget is not None else 0.0
    agent_blocks = []
    for binary in agents:
        agent = "claude_code" if binary == "claude" else binary
        model_lines = ""
        try:
            agent_type = AgentType(agent)
        except ValueError:
            agent_type = None
        if agent_type is not None:
            tiers = default_model_tiers_for(agent_type)
            default_tier = tiers.get(DEFAULT_MODEL_TIER)
            if default_tier is not None and default_tier.model:
                model_lines += f"    model: {default_tier.model}\n"
                if default_tier.reasoning_effort:
                    model_lines += f"    reasoning_effort: {default_tier.reasoning_effort}\n"
            if tiers:
                model_lines += "    model_tiers:\n"
                for tier_name, tier_cfg in tiers.items():
                    model_lines += f"      {tier_name}:\n"
                    model_lines += f"        enabled: {'true' if tier_cfg.enabled else 'false'}\n"
                    if tier_cfg.model:
                        model_lines += f"        model: {tier_cfg.model}\n"
                    if tier_cfg.reasoning_effort:
                        model_lines += f"        reasoning_effort: {tier_cfg.reasoning_effort}\n"
        pricing_lines = "\n".join(_AGENT_PRICING_LINES.get(agent, ()))
        if pricing_lines:
            model_lines += f"{pricing_lines}\n"
        agent_blocks.append(f"  {agent}:\n    enabled: true\n    binary: {binary}\n{model_lines}")
    agent_text = "".join(agent_blocks)
    agents_section = f"agents:\n{agent_text}" if agent_text else "agents: {}\n"
    return (
        "# Auto-generated by AgentShore.  Edit to customise.\n"
        "project:\n"
        "  path: .\n"
        "  goals: null\n"
        "github:\n"
        f"  repo: {name_with_owner}\n"
        f"{agents_section}"
        "budget:\n"
        f"  enabled: {'true' if budget_enabled else 'false'}\n"
        f"  total: {budget_total:.2f}\n"
        "trusted_ids:\n"
        "  github_logins: []\n"
        "rl:\n"
        "  policy_mode: learning\n"
        "  reverse_failsafe_enabled: false\n"
        "  reverse_failsafe_after_idle_ticks: 3\n"
        "  stale_idle_claim_release_ticks: 3\n"
        "  update_every: 16\n"
        "scope:\n"
        f"  strict_mode: {'true' if strict else 'false'}\n"
        "feedback:\n"
        "  cadence_plays: null\n"
        "  cadence_minutes: null\n"
        "skills:\n"
        "  install_on_start: true\n"
        "  path: .agents/skills/\n"
        "  context_file: .agentshore/context.json\n"
    )


def _get_db_path(project: str | None) -> Path:
    """Return the path to the AgentShore database for *project*."""
    base = Path(project) if project else Path(".")
    return base / _PROJECT_DIR / "agentshore.db"
