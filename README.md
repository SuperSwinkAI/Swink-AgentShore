# AgentShore™

[![CI](https://github.com/SuperSwinkAI/Swink-AgentShore/actions/workflows/ci.yml/badge.svg)](https://github.com/SuperSwinkAI/Swink-AgentShore/actions/workflows/ci.yml) [![PyPI version](https://img.shields.io/pypi/v/agentshore)](https://pypi.org/project/agentshore/) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

**RL-based multi-agent coding orchestrator.** AgentShore™ runs a reinforcement learning policy that selects "plays" — discrete skills like issue pickup, code review, QA, and cleanup — and dispatches them to Claude, Codex, Grok, or Antigravity agents working a GitHub issue backlog. You steer via GitHub issues; AgentShore handles the progression.

## What it does

- Picks up GitHub issues, implements them, opens PRs, reviews them, runs QA, and merges
- Uses PPO (proximal policy optimization) to learn which plays to run and when
- Coordinates multiple agents (Claude Code, Codex CLI, Grok CLI, Antigravity CLI) with different GitHub identities so code review is always done by a different agent than the one that wrote the code
- Keeps humans in the loop via the GitHub issue tracker — no AgentShore-specific approval UI needed

## Install

```bash
pip install agentshore
```

Requires Python 3.12+. The wheel is self-contained — schema, dashboard assets, and skill templates are all bundled, so a plain `pip install` yields a fully working CLI with no extras required.

For development from a checkout, `uv sync --group dev` sets up the full toolchain in `.venv/` and you can run the CLI with `uv run agentshore`.

### Windows 11

The `agentshore` CLI runs on Windows 11. In addition to `pip install agentshore`, a bootstrap script is available in the repo that locates or installs `uv` and then runs `uv tool install`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-agentshore.ps1
```

If a corporate/AV HTTPS-inspection proxy breaks downloads, the script passes `uv`'s `--native-tls` for you; for bare `pip`, point it at your system CA bundle.

The macOS desktop app (Tauri shell + bundled `bd` sidecar + Python wheel) is built and signed by `uv run python -m scripts.buildkit macos`, which produces a signed `.app`, `.dmg`, and `.pkg` installer. The Windows desktop app is built by `uv run python -m scripts.buildkit windows`, which produces a machine-wide Inno Setup wizard `.exe` with matching Desktop, Timelapse Capture, and CLI component choices. Both run from the repo root through the cross-platform build spine in `scripts/buildkit/`.

## Quick start

Choose the path that matches how you want to run AgentShore:

- CLI from a checkout or Python install: [`docs/getting-started-cli.md`](https://github.com/SuperSwinkAI/Swink-AgentShore/blob/main/docs/getting-started-cli.md)
- macOS desktop app or `.pkg` installer: [`docs/getting-started-desktop.md`](https://github.com/SuperSwinkAI/Swink-AgentShore/blob/main/docs/getting-started-desktop.md)

Both paths end in selecting a project, configuring agents and identities, and starting a supervised session.

## Requirements

- Python 3.12+
- `gh` CLI authenticated (`gh auth login`)
- One or more agent CLIs on PATH: `claude`, `codex`, `grok`, `agy` (Antigravity)
- A GitHub repository with issues

## Configuration

`agentshore init` generates `agentshore.yaml` in your project root. The source of truth for fields and defaults is `src/agentshore/config/models.py` plus `_DEFAULT_YAML` in `src/agentshore/config/__init__.py`.

Re-run `agentshore init` at any time to refresh settings via the setup wizards (it
preserves your existing `agentshore.yaml` unless you pass `--force`).

## CLI reference

Registered subcommands are `init`, `start`, `stop`, `dashboard`, `identity`, and `trusted-ids`. Use `agentshore <subcommand> --help` for option details.

## Architecture

The core loop: observe state → RL policy selects a play → execute play via agent → compute reward → update policy.

- **RL engine**: custom PPO in PyTorch, 22-action head (19 active plays + 3 reserved/masked, action-space version 13), 246-feature observation vector (observation version 13)
- **Plays**: each play implements `preconditions()`, `execute()`, `estimated_cost()`; a mask prevents invalid plays from being selected
- **Agents**: CLI agents (Claude Code, Codex, Grok, Antigravity) run as async subprocesses
- **Three-layer graph**: BEADS is the canonical project graph (epics → stories → tasks), GitHub is the human conversation surface, and AgentShore's SQLite database holds session-scoped RL state
- **Data**: single SQLite database per project (schema version 4, 22 tables), WAL mode, aiosqlite

Design documentation: [`docs/design/HLD.md`](https://github.com/SuperSwinkAI/Swink-AgentShore/blob/main/docs/design/HLD.md)

## Dashboard

Run `agentshore start --headless`, then `agentshore dashboard` for a live session. For dashboard-only development, run the Vite app in `dashboard/` and open the demo transport with `?demo=1`.

## Contributing

See [CONTRIBUTING.md](https://github.com/SuperSwinkAI/Swink-AgentShore/blob/main/CONTRIBUTING.md).

## License

MIT — Copyright © 2026 SuperSwinkAI

AgentShore™ and Swink™ are trademarks of SuperSwinkAI, pending registration.
