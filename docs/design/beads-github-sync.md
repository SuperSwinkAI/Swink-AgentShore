# Beads GitHub Sync — Evaluation

Status: **Evaluation — reject wholesale adoption, no partial adoption without further work** ·
Branch: `beads_improvements` · Related: `src/agentshore/core/issue_syncer.py`,
`src/agentshore/plays/skill_backed/_merge_reconcile.py`,
`src/agentshore/skills/templates/agentshore-calibrate-alignment/SKILL.md`, #279.

## Question

AgentShore hand-rolls its GitHub↔beads mirror: agents create beads with
`--external-ref "gh-N"`, the orchestrator sweeps duplicate beads
(`issue_syncer.py`), `merge-reconcile` closes beads on PR merge, and
`calibrate-alignment` reconciles bead status against GitHub issue/PR state
every tick. Should any of this be replaced by upstream `bd github sync`
(bidirectional, `--pull-only`/`--push-only`, `--prefer-newer`/`--prefer-github`/
`--prefer-local`, `--dry-run`) or bd's more general tracker system?

## Upstream semantics (`bd github sync`)

Confirmed from the CLI reference
([source](https://raw.githubusercontent.com/gastownhall/beads/main/docs/CLI_REFERENCE.md),
`### bd github` section):

- `bd github sync [--pull-only|--push-only] [--prefer-newer(default)|--prefer-github|--prefer-local] [--issues ids] [--parent id] [--dry-run]` —
  bidirectional by default (pull new/updated from GitHub, then push local
  beads to GitHub).
- `bd github pull [refs...]` is sugar for `sync --pull-only --issues <refs>`;
  `bd github push [ids...]` is sugar for `sync --push-only --issues <ids>`.
- `bd github status` reports last sync timestamp, config status, count of
  issues with GitHub links, and "issues pending push (**no external_ref**)" —
  confirming `external_ref` is the join key bd uses to decide "already
  synced" vs "needs push."
- Config: `github.token`/`GITHUB_TOKEN`, `github.owner`/`GITHUB_OWNER`,
  `github.repo`/`GITHUB_REPO`, `github.repository`/`GITHUB_REPOSITORY`
  (combined `owner/repo`), `github.url`/`GITHUB_API_URL` (Enterprise).

## The matching gap (source-verified, decisive)

Reading `internal/github/tracker.go` directly (not just docs) turns up a hard
incompatibility. bd's GitHub tracker recognizes an `external_ref` as "already
synced" only if it matches one of two patterns:

```go
var issueNumberPattern = regexp.MustCompile(`/issues/(\d+)`)      // full GitHub URL
var ghShorthandPattern = regexp.MustCompile(`^github:([1-9]\d*)$`) // "github:42"

func (t *Tracker) IsExternalRef(ref string) bool {
    if ghShorthandPattern.MatchString(ref) {
        return true
    }
    return strings.Contains(ref, "github.com") && issueNumberPattern.MatchString(ref)
}
```

AgentShore's convention — used everywhere, confirmed in
`agentshore-calibrate-alignment/SKILL.md:17-21` and every skill template that
links a bead (`groom-backlog`, `seed-project`, `design-audit`) — is
`external_ref = "gh-9"`. That string matches **neither** pattern. To bd's own
tracker, every bead AgentShore has ever linked looks identical to a bead with
no `external_ref` at all: "pending push."

Running `bd github push` (or a bidirectional `sync`) against AgentShore's real
store today would therefore attempt to **create a second, duplicate GitHub
issue** for every already-mirrored bead, because bd's matcher has no way to
recognize the existing `gh-N` value as "already synced." This isn't an edge
case — it's the default state of the entire graph.

## Field mapping

From `internal/github/fieldmapper.go`: status maps binary
open↔closed only (`types.StatusOpen`/`StatusClosed`) — there is no
`in_progress`/`blocked`/`deferred` concept on the GitHub side (which matches
GitHub's own issue model; those are beads-only states). Priority and type map
through GitHub labels via a configurable label map. Dependency info is
inferred from GitHub issue body/label text into a generic
`tracker.DependencyInfo`.

## The "tracker-plugin" system, and what it actually is

`internal/tracker/{tracker.go,registry.go}` do define a real Go interface
(`IssueTracker`) with a compile-time factory registry
(`tracker.Register("github", ...)` called from each adapter's `init()`).
GitHub, GitLab, Jira, Linear, Azure DevOps, and Notion each implement it as a
first-party adapter (confirmed: `internal/{github,gitlab,jira,linear,ado,notion}/tracker.go`
all exist). This is presumably what "tracker-plugin system" refers to — but
it is **not** a user-extensible plugin system: there's no dynamic loading and
no way to add a tracker without a bd rebuild. Its only practical effect is
that every first-party tracker gets the identical `{pull,push,sync,status}`
command shape and flag set. It doesn't add any capability beyond what's
already described for GitHub above, and doesn't change the matching-gap
conclusion.

## AgentShore's actual topology vs. what sync can know

GitHub is the human conversation surface and PR-state truth; beads is the
canonical graph; SQLite is session-scoped RL state. The PR-mirror
confirm-then-write principle (never invent GitHub-side state, established
after #279's phantom-PR incident) is a hard constraint here. Three existing
compensators encode facts `bd github sync` structurally cannot see, because it
only reads GitHub *issues*, never PRs:

1. **Duplicate-bead sweep** (`issue_syncer.py:126-171`,
   `_DUPLICATE_BEAD_TITLE_RE`) — closes a GitHub issue when every bead linked
   to it is a closed duplicate. This cleans up AgentShore-specific
   bead-graph duplication (a race between concurrent writers, see
   `beads-server-mode.md`), not GitHub-side staleness. `bd github sync` has
   no concept of "duplicate bead for the same ref" — it would just try to
   reconcile whatever beads carry a recognized `external_ref`.
2. **calibrate-alignment** (`SKILL.md`) — moves a bead to `in_progress` when
   an *open PR* references its issue, and resets an orphaned `in_progress`
   bead back to `open` when its PR closed without merging. Both rules
   require PR visibility. bd's tracker only ever sees binary
   issue-open/closed — it cannot express "in_progress because of an open PR"
   even in principle.
3. **merge-reconcile** (`_merge_reconcile.py`) — closes issues and their
   linked beads specifically at PR-merge time, using AgentShore's own
   PR-link inference (dependency inference first, body-keyword fallback).
   Same gap: PR-merge semantics aren't visible to an issue-only sync.

None of these three could be subsumed by `bd github sync` even after fixing
the `external_ref` format — they encode facts a generic issue-tracker sync
has no way to observe.

## Risk of letting an external sync mutate the graph mid-session

`alignment_delta` is the `global_closure_ratio` delta between orchestrator
ticks, and PPO drives all direction from it (deterministic code only
backstops — never drives or gates). An external `bd github sync` mutating
bead status between ticks — via its own conflict resolution
(`--prefer-newer`/`--prefer-github`) or by closing beads because their
GitHub issue closed — would move that ratio outside the RL loop's own
accounting, producing a delta PPO can't attribute to any play or reward.
Separately, a sync running concurrently with an agent's own skill-template
writes (no shared lock across agent processes, per `beads-server-mode.md`)
could observe a bead mid-construction — e.g. between `bd create
--external-ref gh-9` and the following `bd link` — and either push it
prematurely or, per the matching gap above, never recognize it as synced and
duplicate it.

## Recommendation

**Reject wholesale adoption.** Do not run `bd github sync`, `bd github push`,
or `bd github pull` against AgentShore's live store as currently configured —
the `external_ref` format mismatch alone makes `push` unsafe today (it would
duplicate every previously-mirrored issue). Even after fixing the format,
sync's binary status model can't express the in-progress-via-open-PR /
orphan-reset nuance `calibrate-alignment` already encodes correctly, and an
external mutator writing between orchestrator ticks breaks the
`alignment_delta` attribution PPO depends on.

**Partial adoption worth prototyping, later:** `bd github pull --dry-run` as
a read-only assist feeding candidate signals into `calibrate-alignment` or
`issue_syncer`'s full-sync path — never writing directly. This still needs
the `external_ref` format problem solved first: either teach the AgentShore
side to also recognize `github:N`/URL-form refs (in `pick_bead_for_issue` and
`IssueSyncer`), or accept that `bd github sync` is permanently out of scope
because it can't be taught the `gh-N` pattern without a bd fork.

## Experiment to de-risk before any code changes

Run `bd github sync --pull-only --dry-run` against a scratch clone of a real
AgentShore project's `.beads/` store (never production) and read the dry-run
plan. If it reports something like "0 issues need sync" because it can't
recognize any `gh-N` ref, that confirms the format-incompatibility
conclusion empirically, with no risk, before anyone writes code against this.
