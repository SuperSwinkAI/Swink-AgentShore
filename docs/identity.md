# Per-Agent GitHub Identities

AgentShore can dispatch each CLI coding agent (Claude Code, Codex, Grok,
Antigravity, and future local-LLM CLIs) under a distinct GitHub identity. This attributes PRs,
commits, and reviews to the agent that produced them, and — more importantly —
lets GitHub enforce "review by someone other than the author" at the platform
layer. AgentShore's Code Review play already refuses to let an agent review its
own PR; distinct platform identities make that anti-confirmation guarantee
verifiable in GitHub itself rather than only inside the executor.

Only CLI agents take an identity. API-only agents (those whose key begins with
`api_`) never invoke `gh`, so binding `identity:` on one is rejected at config
parse time.

## Configuration

Identities live in a top-level `identities:` block in `agentshore.yaml`, keyed
by name. Each agent references one by name via its `identity:` field; an
unknown reference is a config error. Every identity supplies git authorship
metadata (`git_user_name`, `git_user_email`) and at most one GitHub token
source — `gh_token_env`, `gh_token_login`, or `gh_token_keychain`. Setting more
than one is rejected.

Two optional fields refine isolation:

- `gh_config_dir` — point this identity at a dedicated `gh` config directory.
  When unset, AgentShore assigns each identity its own isolated config dir so
  concurrent agents never share `gh` state.
- `ssh_key_path` — select a specific SSH key for git operations. The value is
  interpolated into `GIT_SSH_COMMAND`, so whitespace and shell metacharacters
  are rejected.

## Identity resolution

When dispatching a CLI agent subprocess, AgentShore resolves the bound identity
into a per-subprocess environment overlay layered on top of the ambient
environment. The overlay carries git authorship (author and committer name and
email), the resolved token as both `GH_TOKEN` and `GITHUB_TOKEN`, the
(isolated or configured) `GH_CONFIG_DIR`, and, when an SSH key is set, a
`GIT_SSH_COMMAND` pinned to that key. An agent with no identity bound inherits
the user's ambient `gh` auth and contributes no overlay.

## Token sources

Token sources are tried in priority order. The token value itself never appears
in log events — only the source and, after validation, the resolved login.

- **`gh_token_env`** — names an environment variable that holds a PAT, read at
  dispatch time. Suited to headless/CI boxes and scoped fine-grained PATs. The
  field must hold a variable *name*, not a token; a value that looks like a PAT
  is detected and redacted in diagnostics.
- **`gh_token_login`** — looks the token up at runtime via `gh auth token` for
  the named login. The default when the user has run `gh auth login` for each
  account.
- **`gh_token_keychain`** — reads the token from the OS credential store via an
  AgentShore-managed keychain service whose name encodes the login. Suited to
  desktop use: paste once through the wizard, repo-scoped storage thereafter.

## Git credential handling (how the token reaches `git`)

The resolved token authenticates an agent's *own* `git` (push/fetch inside its
worktree) non-interactively. Each agent subprocess runs git under a hardened
env: `GIT_TERMINAL_PROMPT=0` plus an `http.https://github.com/.extraheader`
Basic-auth header derived from that agent's token (the same mechanism
`actions/checkout` uses). Because the header is built per subprocess, each agent
authenticates **as its own identity** — `claude_code` pushes as its login,
`codex` as its login — with no shared-token bleed. Without this hardening an
HTTPS credential prompt has no TTY to answer and the agent hangs to the
wall-clock timeout instead of failing fast (the cause of the codex wall-clock
`issue_pickup` hang).

The **shared worktree fetch** is the one git op with no owning agent — it runs
at allocation time, before any agent is bound. It is read-only (no authorship,
push, or PR), so it uses a single **default git identity** chosen by preference
order: an identity authed via `gh_token_login` (gh OAuth) first, then
`gh_token_keychain`, then `gh_token_env` (PAT), then the first configured
identity. A read-only fetch carries no write/attribution semantics, so a single
read-capable identity here does not affect the per-agent identity invariant. If
no token resolves, the fetch stays unauthenticated (best-effort, falling back to
local refs) — exactly its prior behavior.

A per-identity `git ls-remote` preflight runs at `agentshore start` (after the
identity and CLI-backend-auth preflights) to surface an identity that can't
authenticate to the remote *before* the loop boots, rather than via a mid-run
hang. Bypass with `--skip-git-auth-preflight`.

## Startup requirement: two distinct identities

Because Code Review needs the reviewer's login to differ from the author's,
`agentshore start` fails fast unless at least two distinct GitHub logins are
configured across the enabled CLI agents. Each enabled CLI agent must bind an
`identity:` backed by an explicit token source; identities that fall back to
ambient `gh` auth are rejected, since their resolved login can't be verified at
startup. Configurations with zero enabled CLI agents skip the check — Code
Review is structurally unavailable, so the diversity requirement is moot.

## Verifying and provisioning

`agentshore identity` is a read-only diagnostic. It reports each CLI agent's
identity, token source, and whether GitHub accepted the token (returning a
login), then runs a repository-access preflight for each enabled identity. The
preflight catches a token that is valid for *an* account but scoped to the
wrong repository — a failure that would otherwise only surface when a review
agent runs `gh pr view`. The command exits non-zero if any configured identity
fails to resolve or cannot reach the repo.

`agentshore identity --reconfigure` re-runs the interactive identity wizard
against an existing `agentshore.yaml`. It walks each CLI agent, captures a token
source (gh login, keychain paste, or env var), merges the resulting bindings
back into the config, and leaves the SQLite database untouched. This is the
path to add or fix an identity without re-initializing the project.

## CLI agent backend auth (distinct from GitHub identity)

A GitHub identity is one of two independent credentials each CLI agent needs.
The `identities:` block, token sources, and `agentshore identity` above all
govern the **GitHub identity** — the credential the agent commits, opens PRs,
and merges with. Separately, each CLI agent maintains its own **backend
session** with its model provider: the auth the harness uses to reach the model
itself (for example the Codex CLI's cached `chatgpt.com` session token). That
session carries its own TTL and can expire independently of any GitHub token.
The identity preflight does not see it, so a session with green identity checks
can still have a dead model-provider backend.

A dead backend is uniquely costly: when the Codex CLI's cached token expires it
prints `failed to renew cache TTL` / `failed to refresh available models` to
stderr and then hangs reading from stdin, so every dispatch runs to the full
stream-idle timeout before being killed. AgentShore guards against this on both
sides of a run.

### Pre-launch probe

`agentshore start` probes each configured CLI agent's backend auth right after
the identity preflight, before the loop boots. The probe runs a short,
non-mutating auth-status command per agent type under that agent's resolved
identity env overlay and prints a per-agent banner row. Only an agent type with
a reliable, non-interactive status command is probed today — Codex
(`codex login status`); every other CLI agent reports `unprobeable` and never
blocks. The probe is deliberately conservative: only a *definitively expired*
backend session gates the launch (exit 1, with a `codex login` remediation
hint). Transient outcomes — a probe timeout, a missing binary, or `unprobeable`
— are surfaced as warnings and never strand an otherwise-fine session.

Pass `--skip-auth-preflight` to `agentshore start` to bypass the probe entirely
(for an offline or air-gapped run where the status command can't reach the
provider).

The desktop app runs the same probe as the `check_agent_auth` startup phase and
exposes it live on the agents/identities setup screen as a per-agent backend-auth
badge, so a green badge there provably means the launch gate will pass.

### Runtime recovery backstop

A backend session can also expire *mid-run*, after a clean preflight. When a
CLI agent's dispatch emits a backend-auth signature on stderr, the error
classifier recognises it immediately and aborts that dispatch with
`ErrorClass.AUTH` rather than waiting out the full stream-idle timeout. The
agent lands in `ERROR` and routes through the standard `TAKE_BREAK` recovery —
the same break-then-resume path as a quota/rate-limit hold. A genuinely dead
token fails its breaks and the agent graduates to `END_AGENT` after a bounded
number of failures; the agent *type* is never suppressed — other same-type
agents keep working and a fresh agent of the type can still be spawned. (The
former session-wide type-suppression set was removed: with no re-probe or
decay, one transient blip disabled an entire harness until restart.)

Grok has one additional runtime backstop: if the CLI process starts but never
emits a first stdout byte before the launch-wedge watchdog fires, AgentShore
keeps the dispatch classified as `timeout_stream_idle` and records a bounded,
decaying cooldown for the Grok type (#202) so the fleet routes around it and it
auto-recovers — not a for-the-session suppression.
The first-byte deadline is **600s for all streaming agents** (#213): direct
measurement of the Grok CLI (0.2.32) put `grok-build` time-to-first-byte at
30–70s — far slower than the other CLIs, and dominated by model/relay latency
rather than local startup — and on heavy `code_review` prompts Grok (at the
old 240s grok cap) went silent past its window. Reasoning models legitimately think before the first token, so the
deadline only catches a *broken* child that emits nothing; the 3h wall-clock
backstops genuine hangs. To trim that latency Grok is still dispatched with
`--no-memory --no-plan` (ephemeral single-turn dispatches gain nothing from
cross-session memory or plan mode, and both add latency). Antigravity (`agy`) is
the one exception at 1800s — it is structurally non-streaming and emits no stdout
until its async task completes.

## Windows SSH agent setup

AgentShore uses SSH-signed commits for merge-play provenance. On Windows,
`ssh-add` may not be on PATH even with Git for Windows installed, because
the OpenSSH `ssh-agent` service ships disabled by default.

One-time setup (run once in an elevated PowerShell session):

```powershell
Set-Service ssh-agent -StartupType Manual
Start-Service ssh-agent
ssh-add $env:USERPROFILE\.ssh\<your-key>
```

With `StartupType Manual` the service persists across reboots; run only
`Start-Service ssh-agent` and `ssh-add` at the start of each session if
you prefer to start it on demand.

If `ssh-add` is absent or the agent is not running, AgentShore emits a
structured warning rather than crashing. `agentshore identity` surfaces the
exact fix hint shown above. Merge and review plays that depend on SSH
authorship report the problem clearly; they do not fail silently.

## Constraint: one identity per agent type

Each agent type (claude_code, codex, grok, antigravity) binds to exactly one
GitHub identity. Multi-instance pools with a different identity per instance are a
future enhancement.
