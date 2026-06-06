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
uv tool install --editable .   # install the `agentshore` CLI from a checkout
```

For development, `uv sync --group dev` sets up the full toolchain in `.venv/` and you can run the CLI with `uv run agentshore`.

### Windows 11

The `agentshore` CLI runs on Windows 11. Install it either way:

```powershell
# Recommended: the bootstrap script (locates/installs uv, then `uv tool install`)
powershell -ExecutionPolicy Bypass -File scripts\install-agentshore.ps1
# or, against the GitHub source / an explicit wheel:
.\scripts\install-agentshore.ps1 -Wheel dist\agentshore-0.2.1-py3-none-any.whl
```

```powershell
# Or a plain pip install of the wheel into any Python 3.12+ venv:
py -m venv .venv; .venv\Scripts\Activate.ps1
pip install dist\agentshore-0.2.1-py3-none-any.whl
```

The wheel is self-contained (schema, dashboard assets, and skill templates are bundled), so a plain `pip install` yields a fully working CLI — no extras required. Timelapse capture is an optional, separately-provisioned npm/ffmpeg toolchain and is **not** part of the CLI install. If a corporate/AV HTTPS-inspection proxy breaks downloads, the script passes `uv`'s `--native-tls` for you; for bare `pip`, point it at your system CA bundle.

The macOS desktop app (Tauri shell + bundled `bd` sidecar + Python wheel) is built and signed by `scripts/build-macos.sh`, which produces a signed `.app`, `.dmg`, and `.pkg` installer. A packaged Windows desktop installer is tracked separately (issue #66).

## Quick start

```bash
# In your project directory
agentshore init            # scaffold config, wire BEADS + GitHub, set up identity
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

Re-run `agentshore init` at any time to refresh settings via the setup wizards (it
preserves your existing `agentshore.yaml` unless you pass `--force`).

## CLI reference

```
agentshore init              scaffold config, wire BEADS + GitHub, set up identity
agentshore start             start an RL session (TUI or headless)
agentshore stop              gracefully drain and stop a running session
agentshore dashboard         open the browser dashboard for a running session
agentshore identity          manage and verify per-agent GitHub identities
agentshore trusted-ids       manage GitHub logins allowed to unblock plays
```

## Architecture

The core loop: observe state → RL policy selects a play → execute play via agent → compute reward → update policy.

- **RL engine**: custom PPO in PyTorch, 22-action head (19 active plays + 3 reserved/masked, action-space version 13), 246-feature observation vector (observation version 13)
- **Plays**: each play implements `preconditions()`, `execute()`, `estimated_cost()`; a mask prevents invalid plays from being selected
- **Agents**: CLI agents (Claude Code, Codex, Gemini) run as async subprocesses; API agents use httpx
- **Three-layer graph**: BEADS is the canonical project graph (epics → stories → tasks), GitHub is the human conversation surface, and AgentShore's SQLite database holds session-scoped RL state
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
