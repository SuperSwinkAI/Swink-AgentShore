# AGENTS.md

This file provides guidance to Codex CLI when working with code in this repository.

## What This Is

AgentShore is an RL-based orchestrator that coordinates multiple LLM coding agents (Claude Code, Codex CLI, API-based LLMs) via reinforcement learning. A PPO policy network selects "plays" (22-action head, 19 active plays like Seed Project, Issue Pickup, Code Review, Run QA) to progress coding projects via a beads-native epic/story/task graph. AgentShore does not generate code — it decides what to do next and which agent does it.

## Critical: Never Run AgentShore CLI Commands Directly

**Do not invoke any `agentshore` CLI subcommand (`agentshore start`, `agentshore dashboard`, `agentshore report`, etc.) directly from this agent session.** Running these commands can leave background processes (uvicorn servers, asyncio loops, agent subprocesses) running silently after the tool call completes. Those orphaned processes accumulate API calls and have cost hundreds of dollars in wasted spend. If the user asks to start the dashboard or any other AgentShore service, tell them the exact command to run themselves in their terminal rather than running it here.

**Single exception: the `/monitor_run` skill.** When the user directly invokes `/monitor_run` (and only then), the skill body is authorised to run `agentshore stop --project <DIR>` — and nothing else; `/monitor_run` attaches to an already-running session and never starts one. This carve-out applies only to that named skill, only when the user explicitly invokes it, and only for the command it prescribes — every other AgentShore CLI invocation still falls under the rule above.

This is the same rule as the [skill-template direct-usage prohibition](#critical-never-edit-installed-skill-templates-directly) below — they cover the two ways an agent can accidentally diverge a live AgentShore from its source tree.

## Critical: Never Edit Installed Skill Templates Directly

**Do not edit `~/.claude/skills/agentshore-*`, `~/.codex/skills/agentshore-*`, or any other deployed copy of a AgentShore skill template directly.** The canonical source is `src/agentshore/skills/templates/` in this repo; the deployed copies are installed there by `uv tool install --reinstall --editable .` followed by `agentshore init --force` (per the documented deploy workflow). Edits to the installed copies are silently overwritten on the next install and never reach the source tree. If you need to change skill behavior, change the file under `src/agentshore/skills/templates/` and let the deploy step propagate it.

## Build & Development Commands

```bash
uv sync --group dev          # Install all dependencies (including dev tools)
uv run agentshore --help        # Run CLI
uv run pytest tests/         # Run full suite (xdist-parallel, ~75s on 8-core)
uv run pytest tests/test_cli.py::test_cli_help -p no:xdist  # Run a focused test
uv run ruff check src/ tests/        # Lint
uv run ruff format src/ tests/       # Format
uv run mypy src/                     # Type check
```

The venv lives at `.venv/` — created automatically by `uv sync`. The CLI entry point is `agentshore = "agentshore.cli:main"` (Click-based).

## Running Tests Efficiently

Per `pyproject.toml`, the default `addopts` runs the suite under `pytest-xdist` with `-n auto --dist=worksteal`, plus branch coverage and a 180s per-test timeout. This drops the full suite from ~20 min serial to ~75s on an 8-core box.

- **Full suite**: `uv run pytest tests/` — do NOT pass `-o addopts=''` (that wipes xdist + coverage + timeout and pushes the run back to 8+ min).
- **Avoid `-o addopts=''`**: it silently disables xdist parallelism, coverage enforcement, and the per-test timeout. Prefer `-p no:xdist` instead (keeps coverage + timeout).
- **Focused single test/file**: `uv run pytest tests/path/to/test.py::test_name` — runs fine with default addopts in most cases. Add `-p no:xdist` only if xdist startup cost exceeds the test time.
- **Debug a flaky parallel-only failure**: `uv run pytest tests/path -p no:xdist` — forces serial execution while keeping coverage + timeout.
- **Never tail-pipe a long-running pytest** (`| tail -N` buffers until EOF, so a healthy run looks hung). Use `-q --tb=line` for compact output, or redirect to a file.

## Desktop Builds

**A "build" always means `scripts/build-macos.sh` with no flags — never just `uv build`** (which only emits the wheel/sdist). That script produces the shipping artifact: the signed `.app`/`.dmg`/`.pkg` (dashboard + bd sidecar + Tauri shell + Python wheel).

Both `scripts/build-macos.sh` and `scripts/build-windows.ps1` are **thin shims** over a cross-platform Python build spine in `scripts/buildkit/` (`python -m scripts.buildkit <macos|windows>`). The shims bootstrap `uv` and forward flags verbatim — keep invoking the shell scripts, not the spine directly. The spine owns every phase and ends with a **verification gate** (`verify.py`) that asserts the `.app` payload is exactly the expected binaries (no stray/stale ones), the embedded version matches source, and the signature verifies. Shared phases live in `phases.py`; OS-native packaging in `macos.py`/`windows.py`; the Windows cert/signtool carve-out in `_win_signing.ps1`. See `docs/design/build-pipeline-unification.md`.

**Version is single-sourced** from `pyproject.toml [project].version`; the Tauri config, both Cargo manifests, and both `package.json` files are mirrors. Bump by editing `pyproject.toml` then `uv run python -m scripts.buildkit version --write` — CI and a pytest guard fail on drift. Never hand-edit the mirrors.

## Architecture

The system runs as a single asyncio process. The core loop is: observe state → RL policy selects a play → execute play via agent → compute reward → update policy → repeat.

**UI and transport modes, same core**: In solo mode, a Textual TUI renders state. In embedded/headless agent mode, state streams over a Unix domain socket or TCP IPC. Dashboard mode is a browser bridge on top of the same IPC stream. The `StateProvider` protocol (`src/agentshore/state.py`) decouples core from the consumers.

**RL engine** (`src/agentshore/rl/`): Custom PPO policy network in PyTorch. 22-action discrete head (19 active plays + 3 permanently reserved/masked slots, action-space version 13). State vector (246 features, observation version 13) encodes alignment scores, budget, agent states, failure counts, trajectory projections. Policy outputs are masked to prevent invalid plays (e.g., can't review a PR that doesn't exist).

**Plays** (`src/agentshore/plays/`): Each play implements a `Play` protocol with `preconditions()`, `execute()`, and `estimated_cost()`. The RL engine selects the play type; a separate parameter resolver picks which agent/issue/PR. Anti-confirmation bias is a hard invariant for Code Review: the reviewer GitHub identity must differ from the PR author. Run QA validates trunk/default-branch state and is not identity-blocked in the current implementation.

**Agents** (`src/agentshore/agents/`): CLI agents (Claude Code, Codex, Gemini) are asyncio subprocesses. API agents (GPT and other OpenAI-compatible backends) use httpx. The agent manager handles lifecycle, health monitoring, handoff tracking, and context enrichment from session learnings.

**Beads integration**: AgentShore operates on a three-layer architecture. **BEADS** is the canonical project graph (epics → stories → tasks); **GitHub** is the human conversation surface, with each issue/PR mirrored via `external_ref="gh-N"`; **AgentShore SQLite** is the session-scoped RL state (schema namespace `agentshore_dev_v1`, schema version 3). `agentshore init` runs `ensure_bd_installed → bd_init_project → bd_setup_for_agent_types` to wire the layers together. Alignment is tracked as `alignment_delta: float | None` — `None` means beads is not initialised; `0.0` means first tick or no change; a non-zero float is the `global_closure_ratio` delta since the last tick.

**Data** (`src/agentshore/data/`): Single SQLite database per project (aiosqlite, WAL mode). Schema is in `src/agentshore/data/schema.sql` — 22 tables (schema version 3) including `schema_info` (namespace check) and `schema_version` plus 20 domain tables covering sessions, plays, agents, GitHub issues, pull requests, branch activity, review queue, work claims, dispatch replay, external mutations, scope evidence, policy checkpoints, RL experience, handoffs, trajectory snapshots, human feedback, learnings, archives, review patterns, and worktrees. The version/table-count are pinned by `tests/test_schema_fresh_db.py`.

**Scope validation**: After each skill-backed play, `validate_scope()` enforces issue-inflation limits. Artifact drift is not blocked until AgentShore has reliable beads-native path boundaries; existing drift tables are evidence logs for other consumers.

## Design Docs

Comprehensive design documentation lives in `docs/design/`. The PRD cross-links to relevant design docs. Start with `docs/design/HLD.md` for architecture diagrams and the component map — it links to all 13 component design docs. The TUI mockups are in `docs/design/ui/MOCKUPS.md`.

## Key Conventions

- Python 3.12+, `src/` layout, hatch build backend
- `from __future__ import annotations` in every module
- Ruff for linting and formatting (line length 100)
- mypy strict mode
- asyncio for all I/O — no blocking calls in the core loop
- SQLite via aiosqlite (never raw sqlite3)
- structlog → NDJSON for all logging
- YAML for config (`agentshore.yaml`)
- Config is deeply immutable frozen dataclasses; SIGHUP reload swaps the entire instance atomically

## Git Signing

Commits should remain signed. Do not bypass signing with `commit.gpgsign=false`. This repo uses SSH commit signing; if a commit prompts for your SSH key (e.g. `~/.ssh/id_ed25519`), make sure your SSH agent is reachable first. On macOS, `ssh-add --apple-use-keychain ~/.ssh/<your-key>` loads the key from the Keychain; if you sign through a keychain-backed wrapper, point `gpg.ssh.program` at it and ensure it exports `SSH_AUTH_SOCK` before invoking `ssh-keygen`.
