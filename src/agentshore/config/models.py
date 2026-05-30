"""Frozen dataclass models for AgentShore configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def _tuple(value: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value)


def _string_tuple_mapping(
    value: Mapping[str, list[str] | tuple[str, ...]],
) -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType({key: tuple(items) for key, items in value.items()})


class RunMode(StrEnum):
    """How an AgentShore session renders its UI / streams state.

    ``StrEnum`` members serialize as their plain string value so YAML
    round-trip and Click ``Choice`` lists work without adapter code:
    ``RunMode.SOLO == "solo"`` is ``True``.
    """

    SOLO = "solo"
    AGENT = "agent"


class PolicyMode(StrEnum):
    """How AgentShore's PPO policy should select plays and learn during a session."""

    LEARNING = "learning"
    AUDIT_REPLAY = "audit-replay"

    @property
    def ppo_learning_enabled(self) -> bool:
        return self == PolicyMode.LEARNING

    @property
    def greedy_selection(self) -> bool:
        return self == PolicyMode.AUDIT_REPLAY

    @property
    def summary_label(self) -> str:
        if self == PolicyMode.AUDIT_REPLAY:
            return "audit-replay (PPO learning off, greedy policy)"
        return "learning (PPO learning on)"


@dataclass(frozen=True)
class ProjectConfig:
    path: str = "."
    goals: str | None = None
    # The git branch new PRs target and merges land into. ``None`` means
    # "fall back to the repo's GitHub default branch (origin/HEAD)" so
    # existing projects with no key behave identically. Set via the desktop
    # setup wizard, ``agentshore init`` prompt / ``--target-branch`` flag, or
    # the sidecar ``project.set_target_branch`` RPC.
    target_branch: str | None = None


@dataclass(frozen=True)
class AutoDetectConfig:
    detect_agents: bool = True
    detect_github: bool = True
    detect_api_keys: bool = True
    generate_config: bool = True


@dataclass(frozen=True)
class IntakeConfig:
    seed_paths: tuple[str, ...] = ()
    issue_labels_include: tuple[str, ...] = ()
    issue_labels_exclude: tuple[str, ...] = ("wontfix", "duplicate")
    label_prefix: str = "agentshore/"

    def __post_init__(self) -> None:
        object.__setattr__(self, "seed_paths", _tuple(self.seed_paths))
        object.__setattr__(self, "issue_labels_include", _tuple(self.issue_labels_include))
        object.__setattr__(self, "issue_labels_exclude", _tuple(self.issue_labels_exclude))


@dataclass(frozen=True)
class BudgetConfig:
    enabled: bool = False
    total: float = 0.0
    warning_threshold: float = 0.20


@dataclass(frozen=True)
class TrustedIdsConfig:
    github_logins: tuple[str, ...] = ()
    pr_allow_list: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "github_logins", _tuple(self.github_logins))
        object.__setattr__(self, "pr_allow_list", tuple(self.pr_allow_list))


@dataclass(frozen=True)
class ModelTierConfig:
    enabled: bool = True
    model: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class GitHubIdentity:
    """A GitHub identity that can be bound to a CLI agent type.

    The identity supplies git authorship metadata and a GitHub API token
    to subprocesses dispatched for that agent. Exactly one of
    ``gh_token_env``, ``gh_token_login``, or ``gh_token_keychain`` must be set
    when a token is desired; if all are unset, the agent inherits the user's
    ambient ``gh`` auth.
    """

    git_user_name: str
    git_user_email: str
    gh_token_env: str | None = None
    gh_token_login: str | None = None
    gh_token_keychain: str | None = None
    gh_config_dir: str | None = None
    ssh_key_path: str | None = None


@dataclass(frozen=True)
class AgentConfig:
    enabled: bool = True
    binary: str | None = None
    api_base: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    approved_models: tuple[str, ...] = ()
    model_tiers: Mapping[str, ModelTierConfig] = field(default_factory=dict)
    max_context: int = 200000
    cost_per_1k_input: float = 0.003
    cost_per_1k_cached_input: float | None = None
    cost_per_1k_cache_write_input: float | None = None
    cost_per_1k_output: float = 0.015
    timeout: int | None = None
    # 10-minute silence was killing claude_code/codex/gemini agents mid-think
    # on v0.15.2 (desktop-awc) — 3 plays of session 3862999e timed out
    # producing-no-stdout-for-600s. Bumped to 30 minutes so legitimate
    # long-think + tool-loop windows survive while still detecting
    # genuinely-hung agents.
    stream_idle_timeout: int = 1800
    max_output_size: int = 10_000_000
    # Per-line buffer size for asyncio.create_subprocess_exec. CLI agents emit
    # stream-json where a single result line can exceed asyncio's 64KB default
    # (especially for skills like code_review that gather lots of
    # evidence). 4MB gives ample headroom while staying well under
    # max_output_size.
    line_limit_bytes: int = 4_194_304
    extra_flags: tuple[str, ...] = ()
    identity: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "approved_models", _tuple(self.approved_models))
        object.__setattr__(self, "extra_flags", _tuple(self.extra_flags))
        object.__setattr__(self, "model_tiers", MappingProxyType(dict(self.model_tiers)))


@dataclass(frozen=True)
class FreshStartConfig:
    max_plays_before_reset: int = 20
    context_threshold: float = 0.80
    auto_trigger: bool = False


@dataclass(frozen=True)
class AgentSpawnConfig:
    """Limits and pacing for ``instantiate_agent`` plays.

    ``max_per_config`` is the per-(agent_type, model_tier) cap — at most
    this many live agents of any single (type, tier) combination at once.
    With the default of 2, a fully expanded fleet of 4 agent types × 3
    tiers × 2 = up to 24 agents, but PPO rarely fills more than a handful
    of cells; budget enforcement is the practical ceiling. The previous
    global ``max_total`` field was removed (desktop-ty04) — per-(type, tier)
    gating is sufficient and PPO can't starve a cell by concentrating in
    another.
    """

    cooldown_plays: int = 2
    max_per_config: int = 2


@dataclass(frozen=True)
class BootstrapConfig:
    """First-play decision tunables for the bootstrap recipe.

    The bootstrap recipe queues exactly one work play before fleet expansion:
    ``seed_project`` when the user provided a seed input or the backlog is
    small, ``cleanup`` when the backlog already exceeds ``cleanup_threshold``
    open issues (a fresh seed_project on a 200-issue project pays a large
    reconciliation tax for little marginal value).
    """

    cleanup_threshold: int = 50


@dataclass(frozen=True)
class AgentPreferencesConfig:
    affinity: Mapping[str, str] = field(default_factory=dict)
    exclude: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "affinity", MappingProxyType(dict(self.affinity)))
        object.__setattr__(self, "exclude", _string_tuple_mapping(self.exclude))


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failures: int = 3
    window_seconds: int = 300
    cooldown_seconds: int = 60


@dataclass(frozen=True)
class HealthConfig:
    poll_interval_seconds: int = 30
    stale_context_play_threshold: int = 5


@dataclass(frozen=True)
class DataIntegrityConfig:
    """Defense-in-depth against silent SQLite corruption (desktop-jc7p).

    AgentShore is exposed to environmental corruption sources outside its
    control (macOS screen-lock I/O throttling — see desktop-tvsb). The
    integrity monitor runs a periodic ``PRAGMA quick_check`` canary and
    rotates ``VACUUM INTO`` snapshots so that on the next startup we can
    auto-swap to the most recent intact snapshot if the main DB is bad.
    """

    enabled: bool = True
    canary_interval_seconds: int = 300
    snapshot_interval_seconds: int = 300
    snapshot_ring_size: int = 3
    # Explicit wal_checkpoint(PASSIVE) cadence — complements the
    # wal_autocheckpoint pragma so we always have a deterministic flush
    # trigger regardless of write traffic shape (desktop-gkku).
    wal_checkpoint_interval_seconds: int = 30


@dataclass(frozen=True)
class TaskValidationConfig:
    max_files_per_task: int = 5
    max_estimated_minutes: int = 30
    enforce: bool = True


@dataclass(frozen=True)
class RewardConfig:
    alignment_weight: float = 1.0
    issue_throughput_weight: float = 2.0
    cost_weight: float = 0.1
    time_weight: float = 0.05
    completion_bonus: float = 5.0
    stagnation_penalty: float = 0.5
    failure_penalty: float = 1.0
    issue_inflation_penalty: float = 2.0
    anti_confirmation_bonus: float = 0.3
    loop_penalty: float = 1.5
    # Small per-play bonus for "progress plays" (issue_pickup, code_review,
    # merge_pr) on success — biases PPO toward moving issues forward
    # rather than collapsing onto cheap planning loops.
    progress_play_bonus: float = 0.5
    # Larger bonus for a successful QA pass (gated by a 20-play cooldown so
    # PPO can't farm it).
    qa_success_bonus: float = 2.0
    # Dedicated bonus for a successful merge_pr — the terminal-win signal.
    # 5× the generic progress_play_bonus so PPO learns merges (not the work
    # leading up to them) are the goal. Sized above qa_success_bonus to keep
    # merging the strongest single-play reward outside project completion.
    merge_pr_bonus: float = 2.5
    # Multi-agent and velocity shaping (dispatch plays only)
    concurrent_agent_bonus: float = 0.1
    type_diversity_bonus: float = 0.3
    velocity_bonus: float = 0.5
    velocity_bonus_threshold: float = 0.05
    # Tuning knobs
    inflation_window_size: int = 20
    inflation_window_min_plays: int = 5
    stagnation_threshold: int = 5
    cost_clip_ratio: float = 5.0
    time_clip_ratio: float = 5.0


@dataclass(frozen=True)
class PPOConfig:
    clip_epsilon: float = 0.2
    gae_lambda: float = 0.95
    ppo_epochs: int = 4
    mini_batch_size: int = 4
    value_loss_coef: float = 0.5
    max_grad_norm: float = 0.5
    reward_clip_low: float = -10.0
    reward_clip_high: float = 10.0


@dataclass(frozen=True)
class StagnationConfig:
    # Thresholds in WHOLE MINUTES of "all agents idle" wall-clock time. A
    # busy agent resets the counter — these fire only when no agent has been
    # working for the configured number of minutes.
    warn_after: int = 1
    alert_after: int = 3
    pause_after: int = 5


@dataclass(frozen=True)
class LoopDetectionConfig:
    warn_after: int = 3
    force_switch_after: int = 5
    escalate_after: int = 7
    # desktop-85ex: number of consecutive selector-returns-None ticks (with
    # no in-flight work) before the loop emits ``fleet_idle_persistent``. The
    # event fires exactly once per state transition (entering / leaving the
    # persistent-idle window), never per-tick, to avoid the storm pattern
    # documented in ``project_loop_detector_warning_storm`` memory. Tune
    # downward for faster diagnostics, upward to reduce noise on legitimately
    # slow GitHub-polling sessions.
    fleet_idle_threshold: int = 30


@dataclass(frozen=True)
class RLConfig:
    policy_mode: PolicyMode = PolicyMode.LEARNING
    policy_path: str | None = None
    reverse_failsafe_enabled: bool = False
    reverse_failsafe_after_idle_ticks: int = 3
    stale_idle_claim_release_ticks: int = 3
    learning_rate: float = 0.0003
    gamma: float = 0.99
    entropy_coef: float = 0.05
    update_every: int = 16
    checkpoint_every: int = 16
    # Coefficients for the second policy head that picks an agent config when
    # the play head selects INSTANTIATE_AGENT. The config head fires on a
    # minority of steps; raise these if the policy stops exploring config slots.
    config_policy_coef: float = 1.0
    config_entropy_coef: float = 0.05
    velocity_window_size: int = 20
    reward: RewardConfig = field(default_factory=RewardConfig)
    ppo: PPOConfig = field(default_factory=PPOConfig)
    stagnation: StagnationConfig = field(default_factory=StagnationConfig)
    loop_detection: LoopDetectionConfig = field(default_factory=LoopDetectionConfig)


@dataclass(frozen=True)
class SessionConfig:
    max_plays: int | None = None
    timeout_minutes: int | None = None
    auto_alignment_check_every: int = 5
    auto_archive: bool = True
    archive_dir: str = ".agentshore/archives"
    break_duration_minutes: int = 30


@dataclass(frozen=True)
class FeedbackConfig:
    cadence_plays: int | None = None
    cadence_minutes: int | None = None
    on_stagnation: bool = True
    on_budget_exhaustion: bool = True
    on_loop_escalation: bool = True
    on_ambiguous_intake: bool = True
    # When the loop pauses to request human feedback (e.g. loop_detected) and no
    # response arrives within this many seconds, the session auto-stops instead
    # of wedging indefinitely (#9: an unanswered popup blocked the loop ~8h, and
    # the drain RPC can't be serviced while wedged). None disables the backstop.
    unanswered_timeout_seconds: float | None = 120.0
    # Loop-liveness watchdog (#9): the core loop stamps a monotonic heartbeat at
    # the top of every iteration. An independent watchdog task — NOT on the
    # loop's critical path — force-drains the session if that heartbeat has not
    # advanced within this many seconds, catching a hard-frozen loop that the
    # idle/unanswered-pause backstops above can never reach (they require the
    # loop to keep ticking). Distinct from ``unanswered_timeout_seconds``: that
    # covers a loop that is alive but waiting on a human; this covers a loop that
    # has stopped iterating entirely (e.g. a deadlock in the play-mutation
    # promotion path). None disables the watchdog.
    loop_liveness_timeout_seconds: float | None = 600.0


@dataclass(frozen=True)
class ScopeConfig:
    strict_mode: bool = False
    issue_inflation_threshold: float = 2.0
    seed_project_mid_session_issue_ceiling: int = 10


@dataclass(frozen=True)
class UIConfig:
    theme: str = "dark"
    refresh_rate: float = 1.0


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "info"
    file: bool = True
    log_dir: str = ".agentshore/logs"


@dataclass(frozen=True)
class BrowserConfig:
    enabled: bool = True
    base_url: str = "http://localhost:3000"
    file_patterns: tuple[str, ...] = ("*.tsx", "*.css", "*.html", "*.vue", "*.svelte")
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        object.__setattr__(self, "file_patterns", _tuple(self.file_patterns))


@dataclass(frozen=True)
class LearningsConfig:
    enabled: bool = True
    file: str = ".agentshore/learnings.json"
    max_entries: int = 200
    min_confidence: float = 0.3
    decay_after_sessions: int = 5
    inject_into_prompts: bool = True
    max_prompt_entries: int = 20


@dataclass(frozen=True)
class SkillsConfig:
    install_on_start: bool = True
    path: str = ".agents/skills/"
    context_file: str = ".agentshore/context.json"


@dataclass(frozen=True)
class WorktreeConfig:
    """AgentShore-managed git worktree lifecycle knobs (desktop-12g9)."""

    # Seconds a row stays in ``status='stale'`` before the GitHub-poll
    # tick reaps the on-disk worktree and transitions the row to
    # ``reaped``. Default 1h matches the plan's PR-close grace period.
    reap_ttl_seconds: int = 3600


# ---------------------------------------------------------------------------
# Master availability record (~/.config/swink/agentshore/availability.yaml)
# ---------------------------------------------------------------------------
# Persisted inventory of "what's installable / authenticatable on this
# machine." Both the agent-tier picker and the identity wizard refresh +
# read this on every run, so the user-facing candidate lists come from a
# single source instead of being re-detected per prompt. Lives next to
# ~/.config/swink/agentshore/sessions/ and ~/.config/swink/agentshore/weights/.


@dataclass(frozen=True)
class AgentTypeAvailability:
    """One row of the agent inventory."""

    agent_type: str
    binary: str | None
    available_tiers: tuple[str, ...]
    available: bool


@dataclass(frozen=True)
class GhAccountAvailability:
    """One row of the GitHub-account inventory."""

    login: str
    active: bool
    token_via: str  # "gh_token_login" | "keychain" | "env" | "manual"


@dataclass(frozen=True)
class AvailabilityRecord:
    """Snapshot of detected agents + GitHub accounts."""

    last_refreshed: str  # ISO-8601 UTC
    agent_types: tuple[AgentTypeAvailability, ...] = ()
    github_accounts: tuple[GhAccountAvailability, ...] = ()


@dataclass(frozen=True)
class RuntimeConfig:
    project: ProjectConfig = field(default_factory=ProjectConfig)
    auto: AutoDetectConfig = field(default_factory=AutoDetectConfig)
    intake: IntakeConfig = field(default_factory=IntakeConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    trusted_ids: TrustedIdsConfig = field(default_factory=TrustedIdsConfig)
    identities: Mapping[str, GitHubIdentity] = field(default_factory=dict)
    agents: Mapping[str, AgentConfig] = field(default_factory=dict)
    agent_spawn: AgentSpawnConfig = field(default_factory=AgentSpawnConfig)
    bootstrap: BootstrapConfig = field(default_factory=BootstrapConfig)
    fresh_start: FreshStartConfig = field(default_factory=FreshStartConfig)
    agent_preferences: AgentPreferencesConfig = field(default_factory=AgentPreferencesConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    data_integrity: DataIntegrityConfig = field(default_factory=DataIntegrityConfig)
    task_validation: TaskValidationConfig = field(default_factory=TaskValidationConfig)
    rl: RLConfig = field(default_factory=RLConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    learnings: LearningsConfig = field(default_factory=LearningsConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    worktrees: WorktreeConfig = field(default_factory=WorktreeConfig)
    # Global fallback timeout for any agent dispatch (in seconds). When
    # ``play_timeouts`` does not contain a per-play override, this is the
    # cap that ``AgentManager.dispatch`` enforces (modulo per-agent
    # ``AgentConfig.timeout``, which still takes priority if set).
    agent_timeout: int = 1800
    # Per-play-type timeout overrides (seconds). Resolved at dispatch time
    # via ``effective_play_timeout(play_type)``. Keys are
    # ``PlayType.value`` strings (e.g. ``"issue_pickup"``, ``"unblock_pr"``).
    # Plays not listed fall back to ``agent_timeout``. Tracks
    # desktop-3fiu: in session 2b8729bf the fleet hit 4 timeouts in 95
    # minutes on ``issue_pickup``/``unblock_pr`` while smaller plays were
    # nowhere near the global 1800s cap; per-play headroom lets the
    # implementation/unblock plays get more room without raising the
    # ceiling for fast plays.
    play_timeouts: Mapping[str, int] = field(default_factory=dict)
    mode: RunMode = RunMode.SOLO
    socket: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "identities", MappingProxyType(dict(self.identities)))
        object.__setattr__(self, "agents", MappingProxyType(dict(self.agents)))
        object.__setattr__(self, "play_timeouts", MappingProxyType(dict(self.play_timeouts)))

    def effective_play_timeout(self, play_type: str | None) -> int:
        """Resolve the timeout (seconds) to apply for *play_type*.

        Looks up the per-play override in ``play_timeouts`` first; falls
        back to ``agent_timeout`` when the play isn't in the map (or when
        the caller passes ``None`` because dispatch context didn't carry a
        play type — internal lifecycle calls, tests, etc.).
        """
        if play_type is not None:
            override = self.play_timeouts.get(play_type)
            if override is not None:
                return int(override)
        return int(self.agent_timeout)
