# Per-Agent GitHub Identities

AgentShore can dispatch each CLI coding agent (Claude Code, Codex, Gemini, future
local-LLM CLIs) under a different GitHub identity. This lets you attribute
PRs, commits, and reviews to the agent that produced them — and lets
GitHub's branch protection enforce "review by someone other than the
author" at the platform layer rather than only inside AgentShore's executor.

## Token sources

Exactly one of `gh_token_env`, `gh_token_login`, or `gh_token_keychain`
may be set per identity.

| Source             | Mechanism                                                        | When to use                                         |
| ------------------ | ---------------------------------------------------------------- | --------------------------------------------------- |
| `gh_token_login`   | Runs `gh auth token -h github.com -u <login>`, caches per-process | Default; user has `gh auth login` for each account  |
| `gh_token_env`     | Reads `os.environ[<NAME>]` at dispatch time                      | Headless/CI boxes, or scoped fine-grained PATs      |
| `gh_token_keychain`| Reads from OS credential store via `keyring` library             | Desktop; paste-once via wizard, repo-scoped storage |

Tokens never appear in log events. `agentshore start` requires every enabled
CLI agent identity to have an explicit token source so Code Review can
prove there are at least two distinct GitHub logins.

## Constraint: one identity per agent type

Each agent type (claude_code, codex, gemini) binds to exactly one GitHub
identity. Multi-instance pools with different identities per instance are
a future enhancement.
