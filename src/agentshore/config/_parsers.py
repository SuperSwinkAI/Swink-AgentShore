"""Raw TypedDicts and YAML→dataclass parsing logic for agentshore.yaml."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast, overload

if TYPE_CHECKING:
    # Runtime imports stay function-local to avoid the config→state→config.models
    # cycle; this is for annotations only (deferred by `from __future__`).
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
from agentshore.errors import ConfigError
from agentshore.identity_names import canonical_identity_name, is_valid_github_login
from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS

_RawNumber = int | float | str
_RawStrictNumber = int | float


class _RawProject(TypedDict, total=False):
    path: str
    goals: str | None
    target_branch: str | None


class _RawAuto(TypedDict, total=False):
    detect_agents: bool
    detect_github: bool
    detect_api_keys: bool
    generate_config: bool


class _RawIntake(TypedDict, total=False):
    seed_paths: list[str]
    issue_labels_include: list[str]
    issue_labels_exclude: list[str]
    label_prefix: str


class _RawBudget(TypedDict, total=False):
    enabled: bool
    total: _RawStrictNumber
    warning_threshold: _RawStrictNumber
    time_enabled: bool
    time_total_minutes: int


class _RawTrustedIds(TypedDict, total=False):
    github_logins: list[object]
    pr_allow_list: list[object]
    restrict_issues_to_trusted_authors: object


class _RawModelTier(TypedDict, total=False):
    enabled: bool
    model: str | None
    reasoning_effort: str | None
    max: int


_RawModelTiers = dict[str, str | _RawModelTier]


class _RawAgent(TypedDict, total=False):
    enabled: bool
    binary: str | None
    api_base: str | None
    model: str | None
    reasoning_effort: str | None
    approved_models: list[object]
    model_tiers: _RawModelTiers
    max_context: _RawNumber
    timeout: _RawNumber | None
    stream_idle_timeout: _RawNumber
    first_byte_timeout_seconds: _RawNumber | None
    max_output_size: _RawNumber
    line_limit_bytes: _RawNumber
    extra_flags: list[object]
    identity: str | None


class _RawIdentity(TypedDict, total=False):
    git_user_name: str
    git_user_email: str
    gh_token_env: str | None
    gh_token_login: str | None
    gh_token_keychain: str | None
    gh_config_dir: str | None
    ssh_key_path: str | None


_RawIdentities = dict[str, object]


class _RawFreshStart(TypedDict, total=False):
    max_plays_before_reset: int
    context_threshold: _RawNumber
    auto_trigger: bool


class _RawAgentPreferences(TypedDict, total=False):
    affinity: dict[str, str]
    exclude: dict[str, list[str]]


_RawAgents = dict[str, object]


class _RawPlayPacing(TypedDict, total=False):
    standard_cooldown_plays: int


class _RawBootstrap(TypedDict, total=False):
    cleanup_threshold: int


class _RawCircuitBreaker(TypedDict, total=False):
    failures: _RawNumber
    window_seconds: _RawNumber
    cooldown_seconds: _RawNumber


class _RawHealth(TypedDict, total=False):
    poll_interval_seconds: _RawNumber
    stale_context_play_threshold: _RawNumber


class _RawDataIntegrity(TypedDict, total=False):
    enabled: bool
    canary_interval_seconds: _RawNumber
    snapshot_interval_seconds: _RawNumber
    snapshot_ring_size: _RawNumber
    wal_checkpoint_interval_seconds: _RawNumber


class _RawTaskValidation(TypedDict, total=False):
    max_files_per_task: int
    max_estimated_minutes: int
    enforce: bool


class _RawReward(TypedDict, total=False):
    alignment_weight: _RawNumber
    issue_throughput_weight: _RawNumber
    cost_weight: _RawNumber
    time_weight: _RawNumber
    completion_bonus: _RawNumber
    stagnation_penalty: _RawNumber
    failure_penalty: _RawNumber
    issue_inflation_penalty: _RawNumber
    scope_creep_penalty: _RawNumber
    anti_confirmation_bonus: _RawNumber
    loop_penalty: _RawNumber
    progress_play_bonus: _RawNumber
    qa_success_bonus: _RawNumber
    merge_pr_bonus: _RawNumber
    concurrent_agent_bonus: _RawNumber
    type_diversity_bonus: _RawNumber
    velocity_bonus: _RawNumber
    velocity_bonus_threshold: _RawNumber
    inflation_window_size: _RawNumber
    inflation_window_min_plays: _RawNumber
    stagnation_threshold: _RawNumber
    cost_clip_ratio: _RawNumber
    time_clip_ratio: _RawNumber


class _RawPPO(TypedDict, total=False):
    clip_epsilon: _RawNumber
    gae_lambda: _RawNumber
    ppo_epochs: _RawNumber
    mini_batch_size: _RawNumber
    value_loss_coef: _RawNumber
    max_grad_norm: _RawNumber
    reward_clip_low: _RawNumber
    reward_clip_high: _RawNumber


class _RawStagnation(TypedDict, total=False):
    warn_after: int
    alert_after: int
    pause_after: int


class _RawLoopDetection(TypedDict, total=False):
    warn_after: int
    force_switch_after: int
    escalate_after: int
    fleet_idle_threshold: int


class _RawRL(TypedDict, total=False):
    policy_mode: str
    deterministic: bool
    policy_path: str | None
    reverse_failsafe_enabled: bool
    reverse_failsafe_after_idle_ticks: int
    stale_idle_claim_release_ticks: int
    learning_rate: _RawStrictNumber
    gamma: _RawStrictNumber
    entropy_coef: _RawStrictNumber
    update_every: int
    checkpoint_every: int
    reward: _RawReward
    ppo: _RawPPO
    stagnation: _RawStagnation
    loop_detection: _RawLoopDetection


class _RawSession(TypedDict, total=False):
    max_plays: int | None
    auto_alignment_check_every: int
    auto_archive: bool
    archive_dir: str
    break_duration_minutes: int


class _RawFeedback(TypedDict, total=False):
    cadence_plays: int | None
    cadence_minutes: int | None
    on_stagnation: bool
    on_budget_exhaustion: bool
    on_loop_escalation: bool
    on_ambiguous_intake: bool
    unanswered_timeout_seconds: float | None
    loop_liveness_timeout_seconds: float | None
    graceful_drain_timeout_seconds: float | None


class _RawScope(TypedDict, total=False):
    strict_mode: bool
    issue_inflation_threshold: _RawNumber
    seed_project_mid_session_issue_ceiling: int


class _RawUI(TypedDict, total=False):
    theme: str
    refresh_rate: _RawNumber


class _RawLogging(TypedDict, total=False):
    level: str
    file: bool
    log_dir: str


class _RawTimelapse(TypedDict, total=False):
    enabled: bool
    installed: bool


class _RawLearnings(TypedDict, total=False):
    enabled: bool
    file: str
    max_entries: int
    min_confidence: _RawNumber
    decay_after_sessions: int
    inject_into_prompts: bool
    max_prompt_entries: int


class _RawSkills(TypedDict, total=False):
    install_on_start: bool
    path: str
    context_file: str


class _RawWorktrees(TypedDict, total=False):
    reap_ttl_seconds: int
    root: str | None
    min_free_disk_mb: int
    disk_high_water_mb: int
    reap_failed_pr_after_n: int
    max_active_worktrees: int | None


class _RawConfig(TypedDict, total=False):
    project: _RawProject
    auto: _RawAuto
    intake: _RawIntake
    budget: _RawBudget
    trusted_ids: _RawTrustedIds
    identities: _RawIdentities
    agents: _RawAgents
    play_pacing: _RawPlayPacing
    bootstrap: _RawBootstrap
    circuit_breaker: _RawCircuitBreaker
    health: _RawHealth
    data_integrity: _RawDataIntegrity
    task_validation: _RawTaskValidation
    rl: _RawRL
    session: _RawSession
    feedback: _RawFeedback
    scope: _RawScope
    ui: _RawUI
    logging: _RawLogging
    timelapse: _RawTimelapse
    learnings: _RawLearnings
    skills: _RawSkills
    worktrees: _RawWorktrees
    agent_timeout: _RawNumber
    play_timeouts: dict[str, _RawNumber]
    mode: str
    socket: str | None


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


def _parse_project(raw: _RawProject) -> ProjectConfig:
    target_branch = raw.get("target_branch")
    # Normalise whitespace-only values to None so downstream callers can rely
    # on ``cfg.project.target_branch or <fallback>`` without re-checking
    # truthiness. An explicit empty string in YAML is treated as "unset".
    if isinstance(target_branch, str):
        target_branch = target_branch.strip() or None
    return ProjectConfig(
        path=raw.get("path", "."),
        goals=raw.get("goals"),
        target_branch=target_branch,
    )


def _parse_auto(raw: _RawAuto) -> AutoDetectConfig:
    return AutoDetectConfig(
        detect_agents=raw.get("detect_agents", True),
        detect_github=raw.get("detect_github", True),
        detect_api_keys=raw.get("detect_api_keys", True),
        generate_config=raw.get("generate_config", True),
    )


def _parse_intake(raw: _RawIntake) -> IntakeConfig:
    return IntakeConfig(
        seed_paths=tuple(raw.get("seed_paths", [])),
        issue_labels_include=tuple(raw.get("issue_labels_include", [])),
        issue_labels_exclude=tuple(raw.get("issue_labels_exclude", ["wontfix", "duplicate"])),
        label_prefix=raw.get("label_prefix", "agentshore/"),
    )


def _parse_budget(raw: _RawBudget) -> BudgetConfig:
    from agentshore.budget import parse_budget_raw

    return parse_budget_raw(dict(raw))


def _parse_agent(
    name: str, raw: _RawAgent, *, legacy_max_default: int | None = None
) -> AgentConfig:
    timeout_raw = raw.get("timeout")
    first_byte_raw = raw.get("first_byte_timeout_seconds")
    flags_raw = raw.get("extra_flags", ())
    extra_flags = tuple(str(f) for f in flags_raw) if isinstance(flags_raw, list) else ()
    models_raw = raw.get("approved_models", ())
    approved_models = tuple(str(m) for m in models_raw) if isinstance(models_raw, list) else ()
    model_tiers_raw = raw.get("model_tiers", {}) or {}
    model_tiers = _parse_model_tiers(
        model_tiers_raw if isinstance(model_tiers_raw, dict) else {},
        legacy_max_default=legacy_max_default,
    )
    if legacy_max_default is not None:
        model_tiers = _apply_legacy_default_tiers(name, model_tiers, legacy_max_default)
    identity_raw = raw.get("identity")
    identity = canonical_identity_name(str(identity_raw)) if identity_raw is not None else None
    return AgentConfig(
        enabled=raw.get("enabled", True),
        binary=raw.get("binary"),
        api_base=raw.get("api_base"),
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


# Characters disallowed in ``ssh_key_path``. The path is interpolated into a
# ``GIT_SSH_COMMAND`` shell string at agent dispatch time
# (``agentshore.agents.identity._build_overlay``); whitespace would split the
# ``ssh -i`` argument and shell metacharacters would enable command injection
# from a malicious ``agentshore.yaml``. Reject these at config parse time so the
# ``GitHubIdentity`` dataclass is trustworthy by construction.
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
    # Confirm the string is a syntactically valid path. ``Path`` itself never
    # raises for ordinary strings, but going through ``expanduser`` exercises
    # the same normalization path the env overlay will use later.
    Path(value).expanduser()
    return value


def _parse_identities(raw: _RawIdentities) -> dict[str, GitHubIdentity]:
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
        body_raw = cast("_RawIdentity", body)
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


def _parse_trusted_ids(raw: _RawTrustedIds) -> TrustedIdsConfig:
    if raw is None:
        return TrustedIdsConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"trusted_ids must be a mapping, got {type(raw).__name__}")

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
    binary path like ``binary: /opt/bin/gemini`` still validates when the key is
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


def _validate_agent_types(agents: dict[str, AgentConfig]) -> None:
    """Reject any agent entry that maps to no supported CLI agent type.

    AgentShore only runs the built-in CLI agents; an entry whose key is not an
    ``AgentType`` and whose ``binary`` does not resolve to one (a typo'd key, or
    a removed concept) is otherwise silently dropped downstream — it never
    instantiates — so fail fast here with an actionable message.
    """
    from agentshore.state import AgentType

    valid = ", ".join(t.value for t in AgentType)
    for agent_name, agent_cfg in agents.items():
        if _resolve_agent_type(agent_cfg, agent_name) is None:
            raise ConfigError(
                f"agents.{agent_name!r} is not a supported agent. AgentShore runs "
                f"only the built-in CLI agents ({valid}); rename the key to one of "
                f"those, or set agents.{agent_name}.binary to a recognised CLI."
            )


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

    Currently only Gemini has no effort flag.  Top-level ``reasoning_effort``
    and per-tier ``reasoning_effort`` entries are both checked.
    """
    from agentshore.agents.model_tiers import REASONING_EFFORTS  # local to avoid circular

    for agent_name, agent_cfg in agents.items():
        # Every agent has already passed _validate_agent_types, so this resolves.
        resolved = _resolve_agent_type(agent_cfg, agent_name)
        if resolved is None:
            continue
        if REASONING_EFFORTS.get(resolved):
            # This agent type supports effort — nothing to reject.
            continue

        # Agent type has an empty effort vocabulary (e.g. Gemini).
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


def _clamp_tier_max(value: object) -> int:
    """Clamp a raw tier max value to the valid 1–20 range.

    Non-integer or bool values fall back to 1 (the default).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return min(20, max(1, value))


def _parse_model_tiers(
    raw: _RawModelTiers,
    *,
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


def _parse_circuit_breaker(raw: _RawCircuitBreaker) -> CircuitBreakerConfig:
    return CircuitBreakerConfig(
        failures=int(raw.get("failures", 3)),
        window_seconds=int(raw.get("window_seconds", 300)),
        cooldown_seconds=int(raw.get("cooldown_seconds", 60)),
    )


def _parse_health(raw: _RawHealth) -> HealthConfig:
    return HealthConfig(
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 30)),
        stale_context_play_threshold=int(raw.get("stale_context_play_threshold", 5)),
    )


def _parse_data_integrity(raw: _RawDataIntegrity) -> DataIntegrityConfig:
    return DataIntegrityConfig(
        enabled=bool(raw.get("enabled", True)),
        canary_interval_seconds=int(raw.get("canary_interval_seconds", 300)),
        snapshot_interval_seconds=int(raw.get("snapshot_interval_seconds", 300)),
        snapshot_ring_size=int(raw.get("snapshot_ring_size", 3)),
        wal_checkpoint_interval_seconds=int(raw.get("wal_checkpoint_interval_seconds", 30)),
    )


def _parse_agents(
    raw: _RawAgents,
    *,
    legacy_max_default: int | None = None,
) -> tuple[
    dict[str, AgentConfig],
    FreshStartConfig,
    AgentPreferencesConfig,
]:
    fresh_raw = cast("_RawFreshStart", raw.get("fresh_start", {}) or {})
    prefs_raw = cast("_RawAgentPreferences", raw.get("preferences", {}) or {})

    agents: dict[str, AgentConfig] = {}
    for name, agent_raw in raw.items():
        if name in {"fresh_start", "preferences"}:
            continue
        if isinstance(agent_raw, dict):
            agents[name] = _parse_agent(
                name, cast("_RawAgent", agent_raw), legacy_max_default=legacy_max_default
            )

    fresh = FreshStartConfig(
        max_plays_before_reset=fresh_raw.get("max_plays_before_reset", 20),
        context_threshold=float(fresh_raw.get("context_threshold", 0.80)),
        auto_trigger=fresh_raw.get("auto_trigger", False),
    )

    exclude_raw = prefs_raw.get("exclude", {})
    exclude = {
        key: tuple(value) if isinstance(value, list | tuple) else ()
        for key, value in exclude_raw.items()
    }
    prefs = AgentPreferencesConfig(affinity=prefs_raw.get("affinity", {}), exclude=exclude)

    return agents, fresh, prefs


def _parse_reward(raw: _RawReward) -> RewardConfig:
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


def _parse_ppo(raw: _RawPPO) -> PPOConfig:
    return PPOConfig(
        clip_epsilon=float(raw.get("clip_epsilon", 0.2)),
        gae_lambda=float(raw.get("gae_lambda", 0.95)),
        ppo_epochs=int(raw.get("ppo_epochs", 4)),
        mini_batch_size=int(raw.get("mini_batch_size", 4)),
        value_loss_coef=float(raw.get("value_loss_coef", 0.5)),
        max_grad_norm=float(raw.get("max_grad_norm", 0.5)),
        reward_clip_low=float(raw.get("reward_clip_low", -10.0)),
        reward_clip_high=float(raw.get("reward_clip_high", 10.0)),
    )


def _parse_stagnation(raw: _RawStagnation) -> StagnationConfig:
    return StagnationConfig(
        warn_after=raw.get("warn_after", 1),
        alert_after=raw.get("alert_after", 3),
        pause_after=raw.get("pause_after", 5),
    )


def _parse_loop_detection(raw: _RawLoopDetection) -> LoopDetectionConfig:
    return LoopDetectionConfig(
        warn_after=raw.get("warn_after", 3),
        force_switch_after=raw.get("force_switch_after", 5),
        escalate_after=raw.get("escalate_after", 7),
        fleet_idle_threshold=raw.get("fleet_idle_threshold", 30),
    )


def _parse_policy_mode(raw: _RawRL) -> PolicyMode:
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


def _parse_rl(raw: _RawRL) -> RLConfig:
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
        reward=_parse_reward(raw.get("reward", {}) or {}),
        ppo=_parse_ppo(raw.get("ppo", {}) or {}),
        stagnation=_parse_stagnation(raw.get("stagnation", {}) or {}),
        loop_detection=_parse_loop_detection(raw.get("loop_detection", {}) or {}),
    )


def _parse_session(raw: _RawSession) -> SessionConfig:
    return SessionConfig(
        max_plays=raw.get("max_plays"),
        auto_alignment_check_every=raw.get("auto_alignment_check_every", 5),
        auto_archive=raw.get("auto_archive", True),
        archive_dir=raw.get("archive_dir", ".agentshore/archives"),
        break_duration_minutes=raw.get("break_duration_minutes", 30),
    )


def _parse_feedback(raw: _RawFeedback) -> FeedbackConfig:
    return FeedbackConfig(
        cadence_plays=raw.get("cadence_plays"),
        cadence_minutes=raw.get("cadence_minutes"),
        on_stagnation=raw.get("on_stagnation", True),
        on_budget_exhaustion=raw.get("on_budget_exhaustion", True),
        on_loop_escalation=raw.get("on_loop_escalation", True),
        on_ambiguous_intake=raw.get("on_ambiguous_intake", True),
        unanswered_timeout_seconds=raw.get("unanswered_timeout_seconds", 120.0),
        loop_liveness_timeout_seconds=raw.get("loop_liveness_timeout_seconds", 600.0),
        graceful_drain_timeout_seconds=raw.get("graceful_drain_timeout_seconds", None),
    )


def _parse_scope(raw: _RawScope) -> ScopeConfig:
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


def _parse_ui(raw: _RawUI) -> UIConfig:
    theme = raw.get("theme", "dark")
    if theme not in ("dark", "light"):
        raise ConfigError(f"ui.theme must be 'dark' or 'light', got {theme!r}")
    return UIConfig(
        theme=theme,
        refresh_rate=float(raw.get("refresh_rate", 1.0)),
    )


def _parse_logging(raw: _RawLogging) -> LoggingConfig:
    level = raw.get("level", "info")
    valid_levels = ("debug", "info", "warning", "error")
    if level not in valid_levels:
        raise ConfigError(f"logging.level must be one of {valid_levels}, got {level!r}")
    return LoggingConfig(
        level=level,
        file=raw.get("file", True),
        log_dir=raw.get("log_dir", ".agentshore/logs"),
    )


def _parse_timelapse(raw: _RawTimelapse) -> TimelapseConfig:
    return TimelapseConfig(
        enabled=bool(raw.get("enabled", False)),
        installed=bool(raw.get("installed", False)),
    )


def _parse_learnings(raw: _RawLearnings) -> LearningsConfig:
    return LearningsConfig(
        enabled=raw.get("enabled", True),
        file=raw.get("file", ".agentshore/learnings.json"),
        max_entries=raw.get("max_entries", 200),
        min_confidence=float(raw.get("min_confidence", 0.3)),
        decay_after_sessions=raw.get("decay_after_sessions", 5),
        inject_into_prompts=raw.get("inject_into_prompts", True),
        max_prompt_entries=raw.get("max_prompt_entries", 20),
    )


def _parse_skills(raw: _RawSkills) -> SkillsConfig:
    return SkillsConfig(
        install_on_start=raw.get("install_on_start", True),
        path=raw.get("path", ".agents/skills/"),
        context_file=raw.get("context_file", ".agentshore/context.json"),
    )


def _parse_worktrees(raw: _RawWorktrees) -> WorktreeConfig:
    ttl = raw.get("reap_ttl_seconds", 3600)
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


def _parse_task_validation(raw: _RawTaskValidation) -> TaskValidationConfig:
    return TaskValidationConfig(
        max_files_per_task=raw.get("max_files_per_task", 5),
        max_estimated_minutes=raw.get("max_estimated_minutes", 30),
        enforce=raw.get("enforce", True),
    )


def _parse_play_pacing(raw: _RawPlayPacing) -> PlayPacingConfig:
    cooldown = raw.get("standard_cooldown_plays", STANDARD_PLAY_COOLDOWN_PLAYS)
    if not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0:
        raise ConfigError(
            f"play_pacing.standard_cooldown_plays must be a non-negative integer, got {cooldown!r}"
        )
    return PlayPacingConfig(standard_cooldown_plays=cooldown)


def _parse_bootstrap(raw: _RawBootstrap) -> BootstrapConfig:
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


def _build_config(data: _RawConfig) -> RuntimeConfig:
    from agentshore.agents.pricing import load_pricebook

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

    agents_raw = cast("_RawAgents", dict(data.get("agents", {}) or {}))
    agents, fresh_start, prefs = _parse_agents(agents_raw, legacy_max_default=legacy_max_default)
    identities = _parse_identities(cast("_RawIdentities", data.get("identities", {}) or {}))
    trusted_ids_raw = data.get("trusted_ids", {})
    _validate_agent_types(agents)
    _validate_agent_identities(agents, identities)
    _validate_agent_reasoning_efforts(agents)

    mode_raw = data.get("mode", RunMode.SOLO.value)
    try:
        mode = RunMode(mode_raw)
    except ValueError as exc:
        valid = ", ".join(repr(m.value) for m in RunMode)
        raise ConfigError(f"mode must be one of {valid}, got {mode_raw!r}") from exc

    return RuntimeConfig(
        project=_parse_project(data.get("project", {}) or {}),
        auto=_parse_auto(data.get("auto", {}) or {}),
        intake=_parse_intake(data.get("intake", {}) or {}),
        budget=_parse_budget(data["budget"]) if "budget" in data else BudgetConfig(),
        budget_absent="budget" not in data,
        trusted_ids=_parse_trusted_ids(trusted_ids_raw if trusted_ids_raw is not None else {}),
        identities=identities,
        agents=agents,
        pricebook=load_pricebook(),
        play_pacing=_parse_play_pacing(data.get("play_pacing", {}) or {}),
        bootstrap=_parse_bootstrap(data.get("bootstrap", {}) or {}),
        fresh_start=fresh_start,
        agent_preferences=prefs,
        circuit_breaker=_parse_circuit_breaker(data.get("circuit_breaker", {}) or {}),
        health=_parse_health(data.get("health", {}) or {}),
        data_integrity=_parse_data_integrity(data.get("data_integrity", {}) or {}),
        task_validation=_parse_task_validation(data.get("task_validation", {}) or {}),
        rl=_parse_rl(data.get("rl", {}) or {}),
        session=_parse_session(data.get("session", {}) or {}),
        feedback=_parse_feedback(data.get("feedback", {}) or {}),
        scope=_parse_scope(data.get("scope", {}) or {}),
        ui=_parse_ui(data.get("ui", {}) or {}),
        logging=_parse_logging(data.get("logging", {}) or {}),
        timelapse=_parse_timelapse(data.get("timelapse", {}) or {}),
        learnings=_parse_learnings(data.get("learnings", {}) or {}),
        skills=_parse_skills(data.get("skills", {}) or {}),
        worktrees=_parse_worktrees(data.get("worktrees", {}) or {}),
        agent_timeout=int(data.get("agent_timeout", 10800)),
        play_timeouts=_parse_play_timeouts(data.get("play_timeouts")),
        mode=mode,
        socket=data.get("socket"),
    )
