# AgentShore

**RL-based multi-agent coding orchestrator.** AgentShore runs a reinforcement learning policy that selects "plays" — discrete skills like issue pickup, code review, QA, and cleanup — and dispatches them to Claude, Codex, or Gemini agents working a GitHub issue backlog. You steer via GitHub issues; AgentShore handles the progression.

> **Agent Shoring:** offloading coding work to a coordinated fleet of LLM agents, the way nearshoring offloads work to a coordinated team in another timezone. The RL layer is the engagement manager.

## What it does

- Picks up GitHub issues, implements them, opens PRs, reviews them, runs QA, and merges
- Uses PPO (proximal policy optimization) to learn which plays to run and when
- Coordinates multiple agents (Claude Code, Codex CLI, Gemini CLI) with different GitHub identities so code review is always done by a different agent than the one that wrote the code
- Keeps humans in the loop via the GitHub issue tracker — no AgentShore-specific approval UI needed

## Install

```bash
pip install agentshore
```

Or via the macOS `.pkg` installer (includes bundled sidecar and desktop app).

## Quick start

```bash
# In your project directory
agentshore init            # scaffold config, connect GitHub, set up identity
agentshore status          # confirm everything is wired
agentshore start           # start a supervised session (TUI)
```

## Requirements

- Python 3.12+
- `gh` CLI authenticated (`gh auth login`)
- One or more agent CLIs on PATH: `claude`, `codex`, `gemini`
- A GitHub repository with issues

## Configuration

`agentshore init` generates `agentshore.yaml` in your project root. Key sections:

```yaml
project:
  path: .
  goals: null        # optional plain-text goal for the seed play

agents:
  claude_code:
    enabled: true
    model: sonnet    # haiku / sonnet / opus
  codex:
    enabled: true
    reasoning_effort: medium

budget:
  enabled: true
  total: 5.00        # USD hard cap for the session

rl:
  policy_mode: learning   # learning | frozen | random
```

Full config reference: `agentshore configure --help`.

## CLI reference

```
agentshore init              scaffold config and project state
agentshore start             start an RL session (TUI or headless)
agentshore stop              gracefully drain and stop a running session
agentshore status            show session state, agent health, recent plays
agentshore dashboard         open the browser dashboard for a running session
agentshore configure         interactive config wizard
agentshore identity          manage per-agent GitHub identities
agentshore approvals         review and process pending human-approval requests
agentshore archive           archive session data for cross-session analysis
agentshore report            generate an end-of-session HTML report
agentshore train             offline PPO training from archived experience
agentshore trusted-ids       manage GitHub logins allowed to unblock plays
```

## Architecture

The core loop: observe state → RL policy selects a play → execute play via agent → compute reward → update policy.

- **RL engine**: custom PPO in PyTorch, 22-action head (19 active plays), 246-feature observation vector
- **Plays**: each play implements `preconditions()`, `execute()`, `estimated_cost()`; a mask prevents invalid plays from being selected
- **Agents**: Claude Code, Codex, Gemini run as async subprocesses; API agents use httpx
- **Beads integration**: plays operate on a three-layer graph (BEADS epics/stories/tasks → GitHub issues → AgentShore session DB)
- **Data**: single SQLite database per project (schema namespace `agentshore_dev_v1`), WAL mode, aiosqlite

Design documentation: [`docs/design/HLD.md`](docs/design/HLD.md)

## Dashboard

```bash
agentshore start --headless   # start without TUI
agentshore dashboard          # open browser dashboard
```

Or for development:

```bash
cd dashboard && npm run dev   # run yourself
# Open: http://localhost:5173/?demo=1&scenario=active
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — Copyright (c) 2026 SuperSwinkAI
