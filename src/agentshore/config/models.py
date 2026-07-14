"""Frozen dataclass models for AgentShore configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentshore.agents.pricing import PriceBook


def _tuple(value: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value)


def _default_pricebook() -> PriceBook:
    """Deterministic price-book default for a bare ``RuntimeConfig()``.

    Uses the bundled (no global override) table so test/programmatic config
    construction never depends on a developer's global pricing file. The
    on-disk load path (``_build_config``) passes the full ``load_pricebook()``
    so the global override + SIGHUP reload work for real sessions.
    """
    from agentshore.agents.pricing import bundled_pricebook

    return bundled_pricebook()


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
    # Branch new PRs target and merges land into. ``None`` ⇒ fall back to the
    # repo's GitHub default branch (origin/HEAD), so projects with no key are
    # unchanged. Set via desktop wizard, ``agentshore init`` / ``--target-branch``,
    # or the sidecar ``project.set_target_branch`` RPC.
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
    # Wall-clock cap, independent of the dollar cap above. Off ⇒ no time limit;
    # on ⇒ time_total_minutes validated to 60–4320 (1h–72h). 20-min graceful
    # drain mirrors the dollar reserve; the deadline is the backstop.
    time_enabled: bool = False
    time_total_minutes: int = 0


@dataclass(frozen=True)
class TrustedIdsConfig:
    github_logins: tuple[str, ...] = ()
    pr_allow_list: tuple[int, ...] = ()
    restrict_issues_to_trusted_authors: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "github_logins", _tuple(self.github_logins))
        object.__setattr__(self, "pr_allow_list", tuple(self.pr_allow_list))
        object.__setattr__(
            self,
            "restrict_issues_to_trusted_authors",
            bool(self.restrict_issues_to_trusted_authors),
        )


@dataclass(frozen=True)
class ModelTierConfig:
    enabled: bool = True
    model: str | None = None
    reasoning_effort: str | None = None
    max: int = 1


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
    model: str | None = None
    reasoning_effort: str | None = None
    approved_models: tuple[str, ...] = ()
    model_tiers: Mapping[str, ModelTierConfig] = field(default_factory=dict)
    max_context: int = 200000
    timeout: int | None = None
    # Allows long tool loops while still timing out genuinely silent agents.
    stream_idle_timeout: int = 1800
    # Per-agent override for the launch-to-first-byte watchdog (#177/#204);
    # overrides the per-type and global ``_FIRST_BYTE_DEADLINE_S`` floor (still
    # clamped to ``timeout``). ``None`` ⇒ per-type default, then global default.
    first_byte_timeout_seconds: int | None = None
    max_output_size: int = 10_000_000
    # Per-line buffer for create_subprocess_exec; a single stream-json result
    # line can exceed asyncio's 64KB default (e.g. code_review evidence). 4MB
    # gives headroom while staying well under max_output_size.
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
class PlayPacingConfig:
    """Pacing knobs for heavyweight skill-backed plays."""

    standard_cooldown_plays: int = STANDARD_PLAY_COOLDOWN_PLAYS


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
class PreferencesConfig:
    """Machine-global user preferences, folded in from ``preferences.yaml``.

    Sourced from the user-level global file (not ``agentshore.yaml``) at load
    time, so a config reload re-reads it mid-session. ``disabled_plays`` holds
    ``PlayType.value`` strings the user has turned off; only plays in
    :data:`agentshore.preferences.USER_DISABLEABLE_PLAYS` are honored (the mask
    re-checks the allowlist, so a hand-edited file cannot disable a critical
    play). Not to be confused with :class:`AgentPreferencesConfig`, which is the
    per-project play→agent affinity/exclusion map.
    """

    disabled_plays: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "disabled_plays", _tuple(self.disabled_plays))


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
    # Progress-play bonus (issue_pickup, code_review, merge_pr) — biases PPO
    # toward moving issues forward over cheap planning loops.
    progress_play_bonus: float = 0.5
    # QA-pass bonus; gated by the standard play cooldown so PPO can't farm it.
    qa_success_bonus: float = 2.0
    # merge_pr terminal-win signal: 5× progress_play_bonus and above
    # qa_success_bonus so merging is the strongest play reward short of completion.
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
    # Thresholds in WHOLE MINUTES of "all agents idle" wall-clock; a busy agent
    # resets the counter.
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
    # Optional graceful-drain deadline (#180), OPT-IN — defaults to None
    # (unbounded). A graceful drain only dispatches ``end_agent`` and waits for
    # in-flight plays to finish; the core design intent is that a drain is
    # unbounded so agents always complete their work before the session stops.
    # When set to a positive value, an independent watchdog task — NOT on the
    # loop's critical path — escalates to the bounded hard stop (cancels
    # in-flight plays under the shutdown grace period) once that many seconds
    # elapse with plays still in flight. This was added to reap a drain wedged on
    # a single stuck in-flight play (a multi-hour play, or a never-finalizing
    # broken-worktree play) that previously hung ``agentshore stop`` ~1h until
    # SIGINT. It must stay opt-in: a wall-clock cap cannot distinguish a wedged
    # drain from a healthy-but-slow one (e.g. a large fleet draining serially via
    # ``end_agent``), so a non-None default hard-kills in-flight agent work
    # mid-task. Leave None unless a deployment specifically needs the backstop.
    graceful_drain_timeout_seconds: float | None = None


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
class TimelapseConfig:
    """Optional desktop timelapse-capture feature.

    ``installed`` records that the ``timelapse-capture`` CLI and its
    dependencies were provisioned via the desktop install checkbox.
    ``enabled`` is the per-project default for whether a session records a
    timelapse of the dashboard; the desktop Start screen can override it for
    a single run. Capture interval/fps and output location are left to the
    timelapse CLI's own defaults, so there is nothing else to configure here.
    """

    enabled: bool = False
    installed: bool = False


@dataclass(frozen=True)
class LearningsConfig:
    enabled: bool = True
    file: str = ".agentshore/learnings.json"
    max_entries: int = 200
    min_confidence: float = 0.3
    decay_after_sessions: int = 5
    inject_into_prompts: bool = True
    max_prompt_entries: int = 20
    # Jaccard token-overlap at/above which two same-category learnings are
    # merged during session-start consolidation. 0 disables consolidation.
    consolidate_overlap_threshold: float = 0.8
    # Kill-switch for the groom re-distillation (Tier-2) agent compaction: when
    # True, groom is allowed to read the full store and emit a wholesale
    # ``learnings_compacted`` replacement; when False, that path is disabled and
    # only the deterministic session-start consolidate() bounds the store.
    redistill_in_groom: bool = True


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
    # ``reaped``. Default 3h matches the prune play's worktree min-age
    # floor (``wedge_signals.collect_recent_worktree_paths``); a shorter
    # window let the reaper free a ``pickup-<N>`` dir that a long
    # re-dispatch was still using on the same path (#243).
    reap_ttl_seconds: int = 10800

    # Base directory for AgentShore-managed worktrees. ``None`` (default) keeps
    # them project-local under ``<repo>/.agentshore/worktrees/`` (gitignored,
    # same filesystem, never the repo's parent). Set to an absolute path to
    # centralize worktrees elsewhere; per-repo subdirs disambiguate by name.
    root: str | None = None

    # --- disk-pressure governance (build-agnostic; #180) -----------------
    # AgentShore can't dictate what agents build inside a worktree (Rust
    # ``target/`` can dwarf the checkout 100×), but it owns how much disk its
    # own worktree fleet is allowed to consume and how it degrades when the
    # host fills. Conservative defaults are on (a fresh install is protected
    # out of the box); set any knob to ``0``/``None`` in ``agentshore.yaml``
    # to disable it.

    # Pre-dispatch free-disk floor (MiB). When free disk under the worktree
    # root is below this before a dispatch, AgentShore first reaps idle
    # worktrees and, if still below, pauses dispatch instead of allocating
    # into a nearly-full disk. ``0`` disables the guard.
    min_free_disk_mb: int = 2048

    # High-water free-disk target (MiB) for the periodic disk-pressure reaper.
    # When free disk drops below this, idle worktrees are reaped LRU (stale
    # first, then oldest ``active``) until back above it. ``0`` disables.
    disk_high_water_mb: int = 4096

    # Consecutive-failure cap for a PR-scoped worktree before it is dropped to
    # ``stale`` (and reclaimed by the TTL reaper) instead of kept warm. The
    # first failure keeps the worktree active for a cheap retry; a worktree
    # that keeps failing is not a useful warm cache. ``0`` keeps the old
    # always-retain behavior.
    reap_failed_pr_after_n: int = 2

    # Optional absolute cap on concurrently-``active`` worktrees, a coarse
    # safety net beneath the disk-based governor. ``None`` (default) = no cap.
    max_active_worktrees: int | None = None


# ---------------------------------------------------------------------------
# Master availability record (platformdirs user_config_dir/agentshore/availability.yaml)
# ---------------------------------------------------------------------------
# Persisted inventory of "what's installable / authenticatable on this
# machine." Both the agent-tier picker and the identity wizard refresh +
# read this on every run, so the user-facing candidate lists come from a
# single source instead of being re-detected per prompt. Lives beside the
# platform-specific sessions/ and weights/ directories.


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
    # ``True`` when the YAML had NO ``budget:`` block. ``bootstrap.py``
    # uses this flag instead of a value-equality sentinel to decide whether
    # to apply safety defaults. A ``BudgetConfig()`` that was explicitly
    # written to the YAML (e.g., ``agentshore start --unlimited``) should
    # NOT trigger default application on the next plain ``agentshore start``.
    budget_absent: bool = False
    trusted_ids: TrustedIdsConfig = field(default_factory=TrustedIdsConfig)
    identities: Mapping[str, GitHubIdentity] = field(default_factory=dict)
    agents: Mapping[str, AgentConfig] = field(default_factory=dict)
    # Per-model token pricing, loaded from data/pricing.yaml (+ global override).
    # SIGHUP reload rebuilds this from disk so price edits apply mid-session.
    pricebook: PriceBook = field(default_factory=_default_pricebook)
    play_pacing: PlayPacingConfig = field(default_factory=PlayPacingConfig)
    bootstrap: BootstrapConfig = field(default_factory=BootstrapConfig)
    fresh_start: FreshStartConfig = field(default_factory=FreshStartConfig)
    agent_preferences: AgentPreferencesConfig = field(default_factory=AgentPreferencesConfig)
    # Machine-global user preferences (disabled non-critical plays, …) folded in
    # from the global ``preferences.yaml`` at load time. NOT sourced from
    # ``agentshore.yaml``; see ``agentshore.preferences``.
    preferences: PreferencesConfig = field(default_factory=PreferencesConfig)
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
    timelapse: TimelapseConfig = field(default_factory=TimelapseConfig)
    learnings: LearningsConfig = field(default_factory=LearningsConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    worktrees: WorktreeConfig = field(default_factory=WorktreeConfig)
    # Maximum agent runtime — the absolute wall-clock backstop for any single
    # dispatch (in seconds), default 3h. This is NOT the primary timeout: a
    # genuinely working agent that keeps streaming output runs until this cap;
    # the primary kill is ``AgentConfig.stream_idle_timeout`` (silence, 30 min
    # default), which resets on every stdout byte. This wall-clock only fires
    # when an agent runs the full duration without finishing — its job is to
    # bound a runaway that streams forever (e.g. a noise/poll loop making zero
    # model calls, which defeats the byte-based idle watchdog). User-configurable
    # via ``agent_timeout`` in agentshore.yaml; ``play_timeouts`` overrides it
    # per play type, and per-agent ``AgentConfig.timeout`` still wins if set.
    agent_timeout: int = 10800
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
