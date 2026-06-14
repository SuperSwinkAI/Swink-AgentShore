# CLI Getting Started

Use this path when you want to run AgentShore from a terminal, from a source checkout, or from an installed Python package. If you want the packaged macOS app instead, use the [desktop getting-started guide](getting-started-desktop.md).

## Prerequisites

- Python 3.12 or newer.
- `uv` for local development and editable installs.
- Git and the GitHub CLI, with `gh auth login` completed.
- A GitHub repository with issues for AgentShore to work.
- One or more supported coding agent CLIs on your `PATH`, such as `claude`, `codex`, `gemini`, or `grok`.
- Distinct GitHub identities if you want AgentShore to use the full implement, review, and merge workflow. Code review must run as a different identity from the PR author.

## Install

From an AgentShore checkout:

```bash
uv tool install --editable .
```

For development work inside the checkout:

```bash
uv sync --group dev
uv run agentshore --help
```

The installed command is `agentshore`. In a development checkout, use `uv run agentshore ...` when you want to run against the local source tree.

## Initialize A Project

Run initialization from the repository that AgentShore should operate on:

```bash
cd /path/to/project
agentshore init
```

The setup flow creates or updates `agentshore.yaml`, wires the local project to the AgentShore database, configures GitHub and beads integration, and records the agent types, model tiers, budget settings, and target branch policy for the project.

You can re-run `agentshore init` to refresh settings through the setup wizards. Existing configuration is preserved unless you explicitly use `--force`.

## Check Identities

Before starting long-running work, verify the GitHub identities AgentShore will trust:

```bash
agentshore identity
agentshore trusted-ids
```

Use `agentshore trusted-ids` to review which agent identities are allowed to make changes. Identity checks are intentional guardrails: a blocked merge or review due to an untrusted author usually means the policy is protecting the repository, not that the run is broken.

## Start A Session

Start AgentShore from the target project directory:

```bash
agentshore start
```

The terminal UI shows the active plays, available agents, budget status, and current project state. AgentShore observes the repository, chooses plays with its PPO policy, dispatches supported agents, and records outcomes in the project database.

Before the loop boots, `agentshore start` also checks each CLI agent's **backend session** — the auth its harness uses to reach its model provider, separate from the GitHub identity above. (For Codex this is its cached `chatgpt.com` login; it carries a TTL and can expire between runs.) The check prints a per-agent banner row; if an agent's backend session has expired it stops with an error and a fix hint:

```text
Error: CLI agent backend session expired/dead: codex. Re-authenticate before
starting (e.g. run 'codex login'), ...
```

Re-authenticate the named agent (for example `codex login`) and start again. Only a definitively expired session blocks startup — a probe that can't confirm one way or the other is reported as a warning and does not stop the run. Pass `--skip-auth-preflight` to bypass the check entirely for an offline or air-gapped run.

For a browser dashboard, run the core loop in one terminal and the dashboard bridge in another:

```bash
agentshore start --headless
```

```bash
agentshore dashboard
```

## Stop A Session

Stop a running project session explicitly when you are done:

```bash
agentshore stop --project /path/to/project
```

Use a normal stop first so AgentShore can drain active work and update session state cleanly. Reserve hard process cleanup for cases where the normal stop path cannot reach the session.

## Next Steps

- Configuration reference: [`../README.md#configuration`](../README.md#configuration)
- Identity model: [`identity.md`](identity.md)
- Architecture overview: [`design/HLD.md`](design/HLD.md)
