"""YAML migrations and domain validation for agentshore.yaml."""

from __future__ import annotations

import dataclasses
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload

if TYPE_CHECKING:
    from agentshore.state import AgentType

from agentshore.config.models import (
    AgentConfig,
    AgentPreferencesConfig,
    AutoDetectConfig,
    BootstrapConfig,
    BudgetConfig,
    CircuitBreakerConfig,
    DataIntegrityConfig,
    FeedbackConfig,
    FreshStartConfig,
    GitHubIdentity,
    HealthConfig,
    IntakeConfig,
    LearningsConfig,
    LoggingConfig,
    LoopDetectionConfig,
    ModelTierConfig,
    PlayPacingConfig,
    PolicyMode,
    PPOConfig,
    ProjectConfig,
    RewardConfig,
    RLConfig,
    RunMode,
    RuntimeConfig,
    ScopeConfig,
    SessionConfig,
    SkillsConfig,
    StagnationConfig,
    TaskValidationConfig,
    TimelapseConfig,
    TrustedIdsConfig,
    UIConfig,
    WorktreeConfig,
)
from agentshore.config.wire import mapping, reject_unknown_fields, structure
from agentshore.errors import ConfigError
from agentshore.identity_names import canonical_identity_name, is_valid_github_login
from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS

WireMapping = Mapping[str, Any]


@overload
def _agent_default(name: str, key: str, fallback: float | int) -> float | int: ...


@overload
def _agent_default(name: str, key: str, fallback: None) -> float | int | None: ...


def _agent_default(name: str, key: str, fallback: float | int | None) -> float | int | None:
    """Per-agent-type default for non-priced agent fields (currently max_context).

    Sourced from the bundled price book's ``agent_defaults`` so the one place
    that carries per-agent model metadata is ``pricing.yaml``.
    """
    from agentshore.agents.pricing import bundled_pricebook

    entry = bundled_pricebook().agent_defaults.get(name)
    if entry is None:
        return fallback
    value = getattr(entry, key, fallback)
    return fallback if value is None else value


def _parse_project(raw: WireMapping) -> ProjectConfig:
    parsed = structure(ProjectConfig, raw, "project")
    target_branch = parsed.target_branch
    # Whitespace-only → None so callers can rely on ``target_branch or <fallback>``;
    # an explicit empty string in YAML means "unset".
    if isinstance(target_branch, str):
        target_branch = target_branch.strip() or None
    return dataclasses.replace(parsed, target_branch=target_branch)


def _parse_auto(raw: WireMapping) -> AutoDetectConfig:
    return structure(AutoDetectConfig, raw, "auto")


def _parse_intake(raw: WireMapping) -> IntakeConfig:
    return structure(IntakeConfig, raw, "intake")


def _parse_budget(raw: WireMapping) -> BudgetConfig:
    from agentshore.budget import parse_budget_raw

    reject_unknown_fields(BudgetConfig, raw, "budget")
    return parse_budget_raw(dict(raw))


def _parse_agent(
    name: str, raw: WireMapping, *, legacy_max_default: int | None = None
) -> AgentConfig:
    path = f"agents.{name}"
    reject_unknown_fields(AgentConfig, raw, path)
    timeout_raw = raw.get("timeout")
    first_byte_raw = raw.get("first_byte_timeout_seconds")
    flags_raw = raw.get("extra_flags", ())
    extra_flags = tuple(str(f) for f in flags_raw) if isinstance(flags_raw, list) else ()
    models_raw = raw.get("approved_models", ())
    approved_models = tuple(str(m) for m in models_raw) if isinstance(models_raw, list) else ()
    model_tiers_raw = mapping(raw.get("model_tiers"), f"{path}.model_tiers")
    model_tiers = _parse_model_tiers(
        model_tiers_raw,
        path=f"{path}.model_tiers",
        legacy_max_default=legacy_max_default,
    )
    if legacy_max_default is not None:
        model_tiers = _apply_legacy_default_tiers(name, model_tiers, legacy_max_default)
    identity_raw = raw.get("identity")
    identity = canonical_identity_name(str(identity_raw)) if identity_raw is not None else None
    return AgentConfig(
        enabled=raw.get("enabled", True),
        binary=raw.get("binary"),
        model=raw.get("model"),
        reasoning_effort=raw.get("reasoning_effort"),
        approved_models=approved_models,
        model_tiers=model_tiers,
        max_context=int(raw.get("max_context", _agent_default(name, "max_context", 200_000))),
        timeout=int(timeout_raw) if timeout_raw is not None else None,
        stream_idle_timeout=int(raw.get("stream_idle_timeout", 1800)),
        first_byte_timeout_seconds=(int(first_byte_raw) if first_byte_raw is not None else None),
        max_output_size=int(raw.get("max_output_size", 10_000_000)),
        line_limit_bytes=int(raw.get("line_limit_bytes", 4_194_304)),
        extra_flags=extra_flags,
        identity=identity,
    )


# ssh_key_path is interpolated into a GIT_SSH_COMMAND shell string at dispatch
# (agents.identity._build_overlay); whitespace splits the ``ssh -i`` arg and
# shell metacharacters enable command injection from a malicious agentshore.yaml.
# Rejected at parse time so GitHubIdentity is trustworthy by construction.
_SSH_KEY_PATH_FORBIDDEN_CHARS = frozenset(
    " \t\n\r;&|$`\\\"'(){}<>*?!#",
)


def _validate_ssh_key_path(name: str, value: str) -> str:
    """Validate ``identities.<name>.ssh_key_path`` syntactic shape.

    The value is interpolated into ``GIT_SSH_COMMAND`` via an f-string in the
    identity env overlay, so any whitespace or shell metacharacter would either
    break the command or smuggle additional ssh options / shell commands. We
    reject those characters here. We do *not* resolve symlinks or require the
    file to exist — ``agentshore.yaml`` is often shared across machines and the
    key may legitimately not be provisioned yet on this host. We also do not
    rewrite the stored value so callers see exactly what they wrote (including
    leading ``~``); ``Path(value).expanduser()`` is invoked solely to confirm
    the string is a syntactically valid path.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"identities.{name}.ssh_key_path must be a non-empty string")
    bad = sorted({ch for ch in value if ch in _SSH_KEY_PATH_FORBIDDEN_CHARS})
    if bad:
        rendered = ", ".join(repr(ch) for ch in bad)
        raise ConfigError(
            f"identities.{name}.ssh_key_path contains disallowed character(s) "
            f"{rendered}: {value!r}. Whitespace and shell metacharacters are "
            "rejected because the path is interpolated into GIT_SSH_COMMAND."
        )
    # Exercise the same expanduser normalization the env overlay uses later,
    # confirming the string is a syntactically valid path.
    Path(value).expanduser()
    return value


def _parse_identities(raw: WireMapping) -> dict[str, GitHubIdentity]:
    """Parse the top-level ``identities:`` block.

    Each entry must supply ``git_user_name`` and ``git_user_email``. Identity
    keys are canonicalized with GitHub's case-insensitive login semantics. At
    most one of ``gh_token_env``, ``gh_token_login``, and ``gh_token_keychain``
    may be set; all unset means the agent inherits ambient ``gh`` auth.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"identities must be a mapping, got {type(raw).__name__}")

    out: dict[str, GitHubIdentity] = {}
    for name, body in raw.items():
        raw_name = str(name)
        canonical_name = canonical_identity_name(raw_name)
        if not canonical_name:
            raise ConfigError("identities keys must be non-empty")
        if canonical_name in out:
            raise ConfigError(f"identities contains duplicate case-insensitive key {raw_name!r}")
        if not isinstance(body, dict):
            raise ConfigError(f"identities.{name} must be a mapping, got {type(body).__name__}")
        body_raw = body
        reject_unknown_fields(GitHubIdentity, body_raw, f"identities.{name}")
        git_user_name = body_raw.get("git_user_name")
        git_user_email = body_raw.get("git_user_email")
        if not isinstance(git_user_name, str) or not git_user_name.strip():
            raise ConfigError(f"identities.{name}.git_user_name must be a non-empty string")
        if not isinstance(git_user_email, str) or not git_user_email.strip():
            raise ConfigError(f"identities.{name}.git_user_email must be a non-empty string")

        gh_token_env = body_raw.get("gh_token_env")
        gh_token_login = body_raw.get("gh_token_login")
        gh_token_keychain = body_raw.get("gh_token_keychain")
        token_sources_set = sum(
            1 for v in (gh_token_env, gh_token_login, gh_token_keychain) if v is not None
        )
        if token_sources_set > 1:
            raise ConfigError(
                f"identities.{name}: set at most one of gh_token_env / "
                "gh_token_login / gh_token_keychain"
            )

        ssh_key_path_raw = body_raw.get("ssh_key_path")
        ssh_key_path = (
            _validate_ssh_key_path(str(name), str(ssh_key_path_raw))
            if ssh_key_path_raw is not None
            else None
        )

        out[canonical_name] = GitHubIdentity(
            git_user_name=git_user_name,
            git_user_email=git_user_email,
            gh_token_env=str(gh_token_env) if gh_token_env is not None else None,
            gh_token_login=str(gh_token_login) if gh_token_login is not None else None,
            gh_token_keychain=(str(gh_token_keychain) if gh_token_keychain is not None else None),
            gh_config_dir=(
                str(body_raw["gh_config_dir"])
                if body_raw.get("gh_config_dir") is not None
                else None
            ),
            ssh_key_path=ssh_key_path,
        )
    return out


def _parse_trusted_ids(raw: WireMapping) -> TrustedIdsConfig:
    if raw is None:
        return TrustedIdsConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"trusted_ids must be a mapping, got {type(raw).__name__}")

    reject_unknown_fields(TrustedIdsConfig, raw, "trusted_ids")
    raw_logins = raw.get("github_logins", [])
    if not isinstance(raw_logins, list):
        raise ConfigError(
            f"trusted_ids.github_logins must be a list, got {type(raw_logins).__name__}"
        )

    logins: list[str] = []
    seen: set[str] = set()
    for idx, value in enumerate(raw_logins):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"trusted_ids.github_logins[{idx}] must be a non-empty GitHub login")
        if not is_valid_github_login(value):
            raise ConfigError(
                f"trusted_ids.github_logins[{idx}] is not a valid GitHub login: {value!r}"
            )
        canonical = canonical_identity_name(value)
        if canonical not in seen:
            logins.append(canonical)
            seen.add(canonical)

    raw_pr_allow_list = raw.get("pr_allow_list", [])
    if not isinstance(raw_pr_allow_list, list):
        raise ConfigError(
            f"trusted_ids.pr_allow_list must be a list, got {type(raw_pr_allow_list).__name__}"
        )

    pr_allow_list: list[int] = []
    seen_prs: set[int] = set()
    for idx, value in enumerate(raw_pr_allow_list):
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(
                f"trusted_ids.pr_allow_list[{idx}] must be a positive integer, got {value!r}"
            )
        if value not in seen_prs:
            pr_allow_list.append(value)
            seen_prs.add(value)

    raw_restrict = raw.get("restrict_issues_to_trusted_authors", False)
    if not isinstance(raw_restrict, bool):
        raise ConfigError(
            "trusted_ids.restrict_issues_to_trusted_authors must be a boolean, "
            f"got {type(raw_restrict).__name__}"
        )

    return TrustedIdsConfig(
        github_logins=tuple(logins),
        pr_allow_list=tuple(pr_allow_list),
        restrict_issues_to_trusted_authors=raw_restrict,
    )


def _resolve_agent_type(agent_cfg: AgentConfig, agent_name: str) -> AgentType | None:
    """Resolve an agent entry to its built-in ``AgentType``, or ``None``.

    Prefers the binary→type registry (so a custom key like ``my_claude`` with
    ``binary: claude`` resolves), then falls back to the key itself (so a custom
    binary path like ``binary: /opt/bin/agy`` still validates when the key is
    a canonical type). ``None`` means the entry maps to no supported CLI agent.
    """
    from agentshore.agents.registry import BINARY_TO_AGENT_TYPE
    from agentshore.state import AgentType

    resolved = BINARY_TO_AGENT_TYPE.get(agent_cfg.binary) if agent_cfg.binary else None
    if resolved is not None:
        return resolved
    try:
        return AgentType(agent_name)
    except ValueError:
        return None


def _strip_unsupported_agents(agents: dict[str, AgentConfig]) -> dict[str, AgentConfig]:
    """Drop agent entries that map to no supported CLI agent type, with a warning.

    AgentShore only runs the built-in CLI agents; an entry whose key is not an
    ``AgentType`` and whose ``binary`` does not resolve to one (a typo'd key, or
    a retired concept such as a deprecated provider left in an older config) never
    instantiates downstream. Rather than fail the entire load — which wedges an
    otherwise valid session over a single stale block — such entries are dropped
    and a warning is emitted, leaving the supported agents intact.
    """
    from agentshore.state import AgentType

    kept: dict[str, AgentConfig] = {}
    dropped: list[str] = []
    for agent_name, agent_cfg in agents.items():
        if _resolve_agent_type(agent_cfg, agent_name) is None:
            dropped.append(agent_name)
            continue
        kept[agent_name] = agent_cfg

    if dropped:
        valid = ", ".join(t.value for t in AgentType)
        names = ", ".join(repr(n) for n in dropped)
        plural = len(dropped) > 1
        warnings.warn(
            f"Ignoring unsupported agent {'entries' if plural else 'entry'} {names}: "
            f"not a built-in CLI agent ({valid}). "
            f"{'They were' if plural else 'It was'} dropped from the loaded config; "
            f"rename the key to a supported agent or set its 'binary' to a recognised "
            f"CLI to enable it.",
            stacklevel=2,
        )
    return kept


def _validate_agent_identities(
    agents: dict[str, AgentConfig],
    identities: dict[str, GitHubIdentity],
) -> None:
    """Cross-validate agent ``identity:`` references against the identities map."""
    for agent_name, agent_cfg in agents.items():
        ident = agent_cfg.identity
        if ident is None:
            continue
        if ident not in identities:
            known = ", ".join(sorted(identities)) or "<none>"
            raise ConfigError(
                f"agents.{agent_name}.identity={ident!r} references an unknown "
                f"identity. Known identities: {known}"
            )


def _validate_agent_reasoning_efforts(agents: dict[str, AgentConfig]) -> None:
    """Reject ``reasoning_effort`` on agent types whose CLI has no effort flag.

    Top-level ``reasoning_effort`` and per-tier ``reasoning_effort`` entries are
    both checked.
    """
    from agentshore.agents.model_tiers import REASONING_EFFORTS  # local to avoid circular

    for agent_name, agent_cfg in agents.items():
        # Unsupported agents already stripped upstream; this None guard is defensive.
        resolved = _resolve_agent_type(agent_cfg, agent_name)
        if resolved is None:
            continue
        if REASONING_EFFORTS.get(resolved):
            continue

        # Empty effort vocabulary (e.g. Antigravity) → reject any effort field.
        if agent_cfg.reasoning_effort:
            raise ConfigError(
                f"agents.{agent_name}.reasoning_effort is not supported for "
                f"{resolved.value} (the CLI has no effort flag); remove the field"
            )
        for tier, tier_cfg in agent_cfg.model_tiers.items():
            if tier_cfg.reasoning_effort:
                raise ConfigError(
                    f"agents.{agent_name}.model_tiers.{tier}.reasoning_effort is not "
                    f"supported for {resolved.value} (the CLI has no effort flag); "
                    "remove the field"
                )


def _validate_swink_coding_models(agents: dict[str, AgentConfig]) -> None:
    """Reject swink-coding ``model`` values that are neither a tier alias nor a
    ``provider:model[@endpoint]`` tier-map override.

    Checks both the legacy top-level ``model`` (still honoured as the default
    tier's model when no ``model_tiers`` block is set) and each per-tier
    ``model`` entry. Other agent types are untouched — their model strings are
    opaque to AgentShore.
    """
    from agentshore.agents.cli_swink_coding import classify_swink_model  # local to avoid circular
    from agentshore.state import AgentType

    for agent_name, agent_cfg in agents.items():
        resolved = _resolve_agent_type(agent_cfg, agent_name)
        if resolved is not AgentType.SWINK_CODING:
            continue

        if agent_cfg.model:
            try:
                classify_swink_model(agent_cfg.model)
            except ValueError as exc:
                raise ConfigError(f"agents.{agent_name}.model={agent_cfg.model!r}: {exc}") from exc

        for tier, tier_cfg in agent_cfg.model_tiers.items():
            if not tier_cfg.model:
                continue
            try:
                classify_swink_model(tier_cfg.model)
            except ValueError as exc:
                raise ConfigError(
                    f"agents.{agent_name}.model_tiers.{tier}.model={tier_cfg.model!r}: {exc}"
                ) from exc


def _clamp_tier_max(value: object) -> int:
    """Clamp a raw tier max value to the valid 1–20 range.

    Non-integer or bool values fall back to 1 (the default).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return min(20, max(1, value))


def _parse_model_tiers(
    raw: WireMapping,
    *,
    path: str,
    legacy_max_default: int | None = None,
) -> dict[str, ModelTierConfig]:
    tiers: dict[str, ModelTierConfig] = {}
    if not isinstance(raw, dict):
        return tiers
    default_max = legacy_max_default if legacy_max_default is not None else 1
    for tier, value in raw.items():
        if isinstance(value, str):
            tiers[str(tier)] = ModelTierConfig(model=value, max=default_max)
        elif isinstance(value, dict):
            reject_unknown_fields(ModelTierConfig, value, f"{path}.{tier}")
            raw_max = value.get("max")
            tier_max = _clamp_tier_max(raw_max) if raw_max is not None else default_max
            tiers[str(tier)] = ModelTierConfig(
                enabled=value.get("enabled", True),
                model=value.get("model"),
                reasoning_effort=value.get("reasoning_effort"),
                max=tier_max,
            )
    return tiers


def _apply_legacy_default_tiers(
    agent_name: str,
    parsed: dict[str, ModelTierConfig],
    legacy_max: int,
) -> dict[str, ModelTierConfig]:
    """Materialize an agent's default tiers carrying a migrated legacy cap.

    When a legacy ``agent_spawn.max_per_config`` is migrated, agents that rely
    entirely on default tiers (no ``model_tiers`` block, or only a partial one)
    would otherwise fall back to ``max=1`` and silently lose the old global cap.
    Fill in every default tier the user didn't explicitly configure, carrying
    the migrated ``max`` so the per-(type, tier) ceiling survives the upgrade.

    Agent types with no built-in defaults are returned unchanged.
    """
    import dataclasses

    from agentshore.agents.model_tiers import default_model_tiers_for
    from agentshore.state import AgentType

    try:
        agent_type = AgentType(agent_name)
    except ValueError:
        return parsed
    defaults = default_model_tiers_for(agent_type)
    if not defaults:
        return parsed
    merged = dict(parsed)
    for tier, default_cfg in defaults.items():
        if tier not in merged:
            merged[tier] = dataclasses.replace(default_cfg, max=legacy_max)
    return merged


def _parse_circuit_breaker(raw: WireMapping) -> CircuitBreakerConfig:
    return structure(CircuitBreakerConfig, raw, "circuit_breaker")


def _parse_health(raw: WireMapping) -> HealthConfig:
    return structure(HealthConfig, raw, "health")


def _parse_data_integrity(raw: WireMapping) -> DataIntegrityConfig:
    return structure(DataIntegrityConfig, raw, "data_integrity")


def _parse_agents(
    raw: WireMapping,
    *,
    legacy_max_default: int | None = None,
) -> tuple[
    dict[str, AgentConfig],
    FreshStartConfig,
    AgentPreferencesConfig,
]:
    fresh_raw = mapping(raw.get("fresh_start"), "agents.fresh_start")
    prefs_raw = mapping(raw.get("preferences"), "agents.preferences")

    agents: dict[str, AgentConfig] = {}
    for name, agent_raw in raw.items():
        if name in {"fresh_start", "preferences"}:
            continue
        if isinstance(agent_raw, Mapping):
            agents[name] = _parse_agent(
                name, agent_raw, legacy_max_default=legacy_max_default
            )
        else:
            raise ConfigError(f"agents.{name} must be a mapping, got {type(agent_raw).__name__}")

    fresh = structure(FreshStartConfig, fresh_raw, "agents.fresh_start")

    reject_unknown_fields(AgentPreferencesConfig, prefs_raw, "agents.preferences")
    exclude_raw = prefs_raw.get("exclude", {})
    if not isinstance(exclude_raw, Mapping):
        raise ConfigError(
            f"agents.preferences.exclude must be a mapping, got {type(exclude_raw).__name__}"
        )
    exclude = {
        key: tuple(value) if isinstance(value, list | tuple) else ()
        for key, value in exclude_raw.items()
    }
    affinity_raw = prefs_raw.get("affinity", {})
    if not isinstance(affinity_raw, Mapping):
        raise ConfigError(
            f"agents.preferences.affinity must be a mapping, got {type(affinity_raw).__name__}"
        )
    prefs = AgentPreferencesConfig(affinity=affinity_raw, exclude=exclude)

    return agents, fresh, prefs


# NOTE: kept hand-written, rather than directly structured —
# `issue_inflation_penalty` falls back to the legacy `scope_creep_penalty` key
# when unset, a cross-key aliasing rule the generic defaults-only shape can't
# express.
def _parse_reward(raw: WireMapping) -> RewardConfig:
    reject_unknown_fields(
        RewardConfig, raw, "rl.reward", allow=frozenset({"scope_creep_penalty"})
    )
    return RewardConfig(
        alignment_weight=float(raw.get("alignment_weight", 1.0)),
        issue_throughput_weight=float(raw.get("issue_throughput_weight", 2.0)),
        cost_weight=float(raw.get("cost_weight", 0.1)),
        time_weight=float(raw.get("time_weight", 0.05)),
        completion_bonus=float(raw.get("completion_bonus", 5.0)),
        stagnation_penalty=float(raw.get("stagnation_penalty", 0.5)),
        failure_penalty=float(raw.get("failure_penalty", 1.0)),
        issue_inflation_penalty=float(
            raw.get("issue_inflation_penalty", raw.get("scope_creep_penalty", 2.0))
        ),
        anti_confirmation_bonus=float(raw.get("anti_confirmation_bonus", 0.3)),
        loop_penalty=float(raw.get("loop_penalty", 1.5)),
        progress_play_bonus=float(raw.get("progress_play_bonus", 0.5)),
        qa_success_bonus=float(raw.get("qa_success_bonus", 2.0)),
        merge_pr_bonus=float(raw.get("merge_pr_bonus", 2.5)),
        concurrent_agent_bonus=float(raw.get("concurrent_agent_bonus", 0.1)),
        type_diversity_bonus=float(raw.get("type_diversity_bonus", 0.3)),
        velocity_bonus=float(raw.get("velocity_bonus", 0.5)),
        velocity_bonus_threshold=float(raw.get("velocity_bonus_threshold", 0.05)),
        inflation_window_size=int(raw.get("inflation_window_size", 20)),
        inflation_window_min_plays=int(raw.get("inflation_window_min_plays", 5)),
        stagnation_threshold=int(raw.get("stagnation_threshold", 5)),
        cost_clip_ratio=float(raw.get("cost_clip_ratio", 5.0)),
        time_clip_ratio=float(raw.get("time_clip_ratio", 5.0)),
    )


def _parse_ppo(raw: WireMapping) -> PPOConfig:
    return structure(PPOConfig, raw, "rl.ppo")


def _parse_stagnation(raw: WireMapping) -> StagnationConfig:
    return structure(StagnationConfig, raw, "rl.stagnation")


def _parse_loop_detection(raw: WireMapping) -> LoopDetectionConfig:
    return structure(LoopDetectionConfig, raw, "rl.loop_detection")


def _parse_policy_mode(raw: WireMapping) -> PolicyMode:
    has_policy_mode = "policy_mode" in raw
    has_legacy_deterministic = "deterministic" in raw

    if has_policy_mode:
        mode_raw = raw.get("policy_mode", PolicyMode.LEARNING.value)
        try:
            mode = PolicyMode(mode_raw)
        except ValueError as exc:
            valid = ", ".join(repr(m.value) for m in PolicyMode)
            raise ConfigError(f"rl.policy_mode must be one of {valid}, got {mode_raw!r}") from exc
    else:
        mode = PolicyMode.LEARNING

    if not has_legacy_deterministic:
        return mode

    legacy_value = raw.get("deterministic", False)
    if not isinstance(legacy_value, bool):
        raise ConfigError(f"rl.deterministic must be a boolean, got {legacy_value!r}")
    legacy_mode = PolicyMode.AUDIT_REPLAY if legacy_value else PolicyMode.LEARNING
    if has_policy_mode and legacy_mode != mode:
        raise ConfigError(
            "rl.policy_mode conflicts with legacy rl.deterministic; remove rl.deterministic"
        )
    warnings.warn(
        "rl.deterministic is deprecated; use rl.policy_mode instead",
        DeprecationWarning,
        stacklevel=3,
    )
    return legacy_mode


def _parse_rl(raw: WireMapping) -> RLConfig:
    reject_unknown_fields(RLConfig, raw, "rl", allow=frozenset({"deterministic"}))
    lr = raw.get("learning_rate", 0.0003)
    gamma = raw.get("gamma", 0.99)
    entropy = raw.get("entropy_coef", 0.05)
    if not isinstance(lr, int | float) or lr <= 0:
        raise ConfigError(f"rl.learning_rate must be positive, got {lr!r}")
    if not isinstance(gamma, int | float) or not (0.0 <= gamma <= 1.0):
        raise ConfigError(f"rl.gamma must be between 0.0 and 1.0, got {gamma!r}")
    if not isinstance(entropy, int | float) or entropy < 0:
        raise ConfigError(f"rl.entropy_coef must be non-negative, got {entropy!r}")
    failsafe_ticks = raw.get("reverse_failsafe_after_idle_ticks", 3)
    if not isinstance(failsafe_ticks, int) or failsafe_ticks < 0:
        raise ConfigError(
            "rl.reverse_failsafe_after_idle_ticks must be a non-negative integer, "
            f"got {failsafe_ticks!r}"
        )
    stale_claim_ticks = raw.get("stale_idle_claim_release_ticks", 3)
    if not isinstance(stale_claim_ticks, int) or stale_claim_ticks < 0:
        raise ConfigError(
            "rl.stale_idle_claim_release_ticks must be a non-negative integer, "
            f"got {stale_claim_ticks!r}"
        )

    return RLConfig(
        policy_mode=_parse_policy_mode(raw),
        policy_path=raw.get("policy_path"),
        reverse_failsafe_enabled=raw.get("reverse_failsafe_enabled", False),
        reverse_failsafe_after_idle_ticks=failsafe_ticks,
        stale_idle_claim_release_ticks=stale_claim_ticks,
        learning_rate=float(lr),
        gamma=float(gamma),
        entropy_coef=float(entropy),
        update_every=raw.get("update_every", 16),
        checkpoint_every=raw.get("checkpoint_every", 16),
        config_policy_coef=float(raw.get("config_policy_coef", 1.0)),
        config_entropy_coef=float(raw.get("config_entropy_coef", 0.05)),
        velocity_window_size=int(raw.get("velocity_window_size", 20)),
        reward=_parse_reward(mapping(raw.get("reward"), "rl.reward")),
        ppo=_parse_ppo(mapping(raw.get("ppo"), "rl.ppo")),
        stagnation=_parse_stagnation(mapping(raw.get("stagnation"), "rl.stagnation")),
        loop_detection=_parse_loop_detection(
            mapping(raw.get("loop_detection"), "rl.loop_detection")
        ),
    )


def _parse_session(raw: WireMapping) -> SessionConfig:
    return structure(SessionConfig, raw, "session")


def _parse_feedback(raw: WireMapping) -> FeedbackConfig:
    return structure(FeedbackConfig, raw, "feedback")


def _parse_scope(raw: WireMapping) -> ScopeConfig:
    reject_unknown_fields(ScopeConfig, raw, "scope")
    ceiling_raw = raw.get("seed_project_mid_session_issue_ceiling", 10)
    if isinstance(ceiling_raw, bool) or not isinstance(ceiling_raw, int) or ceiling_raw < 0:
        raise ConfigError(
            "scope.seed_project_mid_session_issue_ceiling must be a non-negative integer, "
            f"got {ceiling_raw!r}"
        )
    return ScopeConfig(
        strict_mode=raw.get("strict_mode", False),
        issue_inflation_threshold=float(raw.get("issue_inflation_threshold", 2.0)),
        seed_project_mid_session_issue_ceiling=ceiling_raw,
    )


def _parse_ui(raw: WireMapping) -> UIConfig:
    reject_unknown_fields(UIConfig, raw, "ui")
    theme = raw.get("theme", "dark")
    if theme not in ("dark", "light"):
        raise ConfigError(f"ui.theme must be 'dark' or 'light', got {theme!r}")
    return UIConfig(
        theme=theme,
        refresh_rate=float(raw.get("refresh_rate", 1.0)),
    )


def _parse_logging(raw: WireMapping) -> LoggingConfig:
    reject_unknown_fields(LoggingConfig, raw, "logging")
    level = raw.get("level", "info")
    valid_levels = ("debug", "info", "warning", "error")
    if level not in valid_levels:
        raise ConfigError(f"logging.level must be one of {valid_levels}, got {level!r}")
    return LoggingConfig(
        level=level,
        file=raw.get("file", True),
        log_dir=raw.get("log_dir", ".agentshore/logs"),
    )


def _parse_timelapse(raw: WireMapping) -> TimelapseConfig:
    return structure(TimelapseConfig, raw, "timelapse")


# NOTE: kept hand-written, rather than directly structured — the wire schema
# deliberately omits `consolidate_overlap_threshold` and `redistill_in_groom`
# (internal-only LearningsConfig knobs, never exposed to agentshore.yaml). A
# fields()-driven helper would start honoring those keys from raw YAML, which
# is a behavior change, not a dedup.
def _parse_learnings(raw: WireMapping) -> LearningsConfig:
    reject_unknown_fields(
        LearningsConfig,
        raw,
        "learnings",
        exclude=frozenset({"consolidate_overlap_threshold", "redistill_in_groom"}),
    )
    return LearningsConfig(
        enabled=raw.get("enabled", True),
        file=raw.get("file", ".agentshore/learnings.json"),
        max_entries=raw.get("max_entries", 200),
        min_confidence=float(raw.get("min_confidence", 0.3)),
        decay_after_sessions=raw.get("decay_after_sessions", 5),
        inject_into_prompts=raw.get("inject_into_prompts", True),
        max_prompt_entries=raw.get("max_prompt_entries", 20),
    )


def _parse_skills(raw: WireMapping) -> SkillsConfig:
    return structure(SkillsConfig, raw, "skills")


def _parse_worktrees(raw: WireMapping) -> WorktreeConfig:
    reject_unknown_fields(
        WorktreeConfig,
        raw,
        "worktrees",
        allow=frozenset({"orphan_retention_seconds"}),
    )
    ttl = raw.get("reap_ttl_seconds", 10800)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0:
        raise ConfigError(f"worktrees.reap_ttl_seconds must be a non-negative integer, got {ttl!r}")
    root = raw.get("root")
    if root is not None and not (isinstance(root, str) and root.strip()):
        raise ConfigError(f"worktrees.root must be a non-empty string or omitted, got {root!r}")

    def _nonneg_int(key: str, default: int) -> int:
        val = raw.get(key, default)
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise ConfigError(f"worktrees.{key} must be a non-negative integer, got {val!r}")
        return val

    min_free_disk_mb = _nonneg_int("min_free_disk_mb", 0)
    disk_high_water_mb = _nonneg_int("disk_high_water_mb", 0)
    reap_failed_pr_after_n = _nonneg_int("reap_failed_pr_after_n", 0)
    max_active = raw.get("max_active_worktrees")
    if max_active is not None and (
        not isinstance(max_active, int) or isinstance(max_active, bool) or max_active < 1
    ):
        raise ConfigError(
            "worktrees.max_active_worktrees must be a positive integer or omitted, "
            f"got {max_active!r}"
        )
    return WorktreeConfig(
        reap_ttl_seconds=ttl,
        root=root.strip() if isinstance(root, str) else None,
        min_free_disk_mb=min_free_disk_mb,
        disk_high_water_mb=disk_high_water_mb,
        reap_failed_pr_after_n=reap_failed_pr_after_n,
        max_active_worktrees=max_active,
    )


def _parse_task_validation(raw: WireMapping) -> TaskValidationConfig:
    return structure(TaskValidationConfig, raw, "task_validation")


def _parse_play_pacing(raw: WireMapping) -> PlayPacingConfig:
    reject_unknown_fields(PlayPacingConfig, raw, "play_pacing")
    cooldown = raw.get("standard_cooldown_plays", STANDARD_PLAY_COOLDOWN_PLAYS)
    if not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0:
        raise ConfigError(
            f"play_pacing.standard_cooldown_plays must be a non-negative integer, got {cooldown!r}"
        )
    return PlayPacingConfig(standard_cooldown_plays=cooldown)


def _parse_bootstrap(raw: WireMapping) -> BootstrapConfig:
    reject_unknown_fields(BootstrapConfig, raw, "bootstrap")
    threshold = raw.get("cleanup_threshold", 50)
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0:
        raise ConfigError(
            f"bootstrap.cleanup_threshold must be a non-negative integer, got {threshold!r}"
        )
    return BootstrapConfig(cleanup_threshold=threshold)


def _parse_play_timeouts(raw: object) -> dict[str, int]:
    """Parse the optional top-level ``play_timeouts`` mapping.

    Accepts a mapping of ``play_type.value`` strings to integer/float seconds
    (the YAML loader sometimes hands us floats). Anything non-mapping or
    non-numeric is rejected via ``ConfigError`` so configuration drift
    surfaces at load time instead of silently masking a play's timeout.
    """

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"play_timeouts must be a mapping of play_type to seconds, got {type(raw).__name__}"
        )
    parsed: dict[str, int] = {}
    for play_type, value in raw.items():
        if not isinstance(play_type, str):
            raise ConfigError(
                f"play_timeouts keys must be strings (PlayType.value), got {play_type!r}"
            )
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(
                f"play_timeouts['{play_type}'] must be a positive number of seconds, got {value!r}"
            )
        parsed[play_type] = int(value)
    return parsed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_config(data: WireMapping) -> RuntimeConfig:
    from agentshore.agents.pricing import load_pricebook

    reject_unknown_fields(
        RuntimeConfig,
        data,
        "config",
        allow=frozenset({"agent_spawn"}),
        exclude=frozenset(
            {"budget_absent", "pricebook", "fresh_start", "agent_preferences", "preferences"}
        ),
    )

    # Migration: legacy agent_spawn block → per-tier max
    legacy_max_default: int | None = None
    agent_spawn_raw = data.get("agent_spawn")
    if isinstance(agent_spawn_raw, dict):
        raw_mpc = agent_spawn_raw.get("max_per_config")
        if isinstance(raw_mpc, int) and not isinstance(raw_mpc, bool) and raw_mpc >= 1:
            legacy_max_default = min(20, max(1, raw_mpc))
        warnings.warn(
            "agent_spawn is deprecated; max_per_config has been migrated to per-tier "
            "'max' on each model tier. Remove the agent_spawn block from "
            "agentshore.yaml to silence this warning.",
            DeprecationWarning,
            stacklevel=2,
        )

    agents_raw = mapping(data.get("agents"), "agents")
    agents, fresh_start, prefs = _parse_agents(agents_raw, legacy_max_default=legacy_max_default)
    agents = _strip_unsupported_agents(agents)
    identities = _parse_identities(mapping(data.get("identities"), "identities"))
    trusted_ids_raw = mapping(data.get("trusted_ids"), "trusted_ids")
    _validate_agent_identities(agents, identities)
    _validate_agent_reasoning_efforts(agents)
    _validate_swink_coding_models(agents)

    mode_raw = data.get("mode", RunMode.SOLO.value)
    try:
        mode = RunMode(mode_raw)
    except ValueError as exc:
        valid = ", ".join(repr(m.value) for m in RunMode)
        raise ConfigError(f"mode must be one of {valid}, got {mode_raw!r}") from exc

    return RuntimeConfig(
        project=_parse_project(mapping(data.get("project"), "project")),
        auto=_parse_auto(mapping(data.get("auto"), "auto")),
        intake=_parse_intake(mapping(data.get("intake"), "intake")),
        budget=(
            _parse_budget(mapping(data["budget"], "budget"))
            if "budget" in data
            else BudgetConfig()
        ),
        budget_absent="budget" not in data,
        trusted_ids=_parse_trusted_ids(trusted_ids_raw),
        identities=identities,
        agents=agents,
        pricebook=load_pricebook(),
        play_pacing=_parse_play_pacing(mapping(data.get("play_pacing"), "play_pacing")),
        bootstrap=_parse_bootstrap(mapping(data.get("bootstrap"), "bootstrap")),
        fresh_start=fresh_start,
        agent_preferences=prefs,
        circuit_breaker=_parse_circuit_breaker(
            mapping(data.get("circuit_breaker"), "circuit_breaker")
        ),
        health=_parse_health(mapping(data.get("health"), "health")),
        data_integrity=_parse_data_integrity(
            mapping(data.get("data_integrity"), "data_integrity")
        ),
        task_validation=_parse_task_validation(
            mapping(data.get("task_validation"), "task_validation")
        ),
        rl=_parse_rl(mapping(data.get("rl"), "rl")),
        session=_parse_session(mapping(data.get("session"), "session")),
        feedback=_parse_feedback(mapping(data.get("feedback"), "feedback")),
        scope=_parse_scope(mapping(data.get("scope"), "scope")),
        ui=_parse_ui(mapping(data.get("ui"), "ui")),
        logging=_parse_logging(mapping(data.get("logging"), "logging")),
        timelapse=_parse_timelapse(mapping(data.get("timelapse"), "timelapse")),
        learnings=_parse_learnings(mapping(data.get("learnings"), "learnings")),
        skills=_parse_skills(mapping(data.get("skills"), "skills")),
        worktrees=_parse_worktrees(mapping(data.get("worktrees"), "worktrees")),
        agent_timeout=int(data.get("agent_timeout", 10800)),
        play_timeouts=_parse_play_timeouts(data.get("play_timeouts")),
        mode=mode,
        socket=data.get("socket"),
    )
