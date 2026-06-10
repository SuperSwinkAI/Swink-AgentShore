# Per-Agent GitHub Identities

AgentShore can dispatch each CLI coding agent (Claude Code, Codex, Gemini, and
future local-LLM CLIs) under a distinct GitHub identity. This attributes PRs,
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

## Windows SSH agent setup

AgentShore uses SSH-signed commits for merge-play provenance. On Windows,
`ssh-add` may not be on PATH even with Git for Windows installed, because
the OpenSSH `ssh-agent` service ships disabled by default.

One-time setup (run once in an elevated PowerShell session):

```powershell
Set-Service ssh-agent -StartupType Manual
Start-Service ssh-agent
ssh-add $env:USERPROFILE\.ssh\id_ed25519
```

With `StartupType Manual` the service persists across reboots; run only
`Start-Service ssh-agent` and `ssh-add` at the start of each session if
you prefer to start it on demand.

If `ssh-add` is absent or the agent is not running, AgentShore emits a
structured warning rather than crashing. `agentshore identity` surfaces the
exact fix hint shown above. Merge and review plays that depend on SSH
authorship report the problem clearly; they do not fail silently.

## Constraint: one identity per agent type

Each agent type (claude_code, codex, gemini) binds to exactly one GitHub
identity. Multi-instance pools with a different identity per instance are a
future enhancement.
