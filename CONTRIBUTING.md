# Contributing to AgentShore™

This guide exists to save both sides time.

## The Standard

**You must understand your code.** If you cannot explain what your changes do and how they interact with the rest of the system, your PR will be closed.

Using AI to write code is fine. Submitting AI-generated code without understanding it is not.

If you use an agent, run it from the repo root so it picks up `CLAUDE.md` / `AGENTS.md` automatically. Your agent must follow the rules in those files.

## Contribution Gate

PRs from new contributors are auto-closed by default. Issues are open to everyone.

Maintainers review auto-closed PRs and reopen worthwhile ones. Reply `lgtm` on any issue or PR from a contributor to grant them PR rights going forward.

## Quality Bar for Issues

Use one of the [GitHub issue templates](https://github.com/SuperSwinkAI/Swink-AgentShore/issues/new/choose).

Keep it short and concrete:

- One screen or less. If it does not fit, it is too long.
- Write in your own voice.
- State the bug or request clearly.
- Explain why it matters to users of this orchestrator.
- If you want to implement the fix yourself, say so.

## Blocking

If you spam the tracker with agent-generated issues or PRs, your GitHub account will be permanently blocked.

## Prerequisites

- **Python 3.12+**. Install via [pyenv](https://github.com/pyenv/pyenv) or your OS package manager.
- **uv** package manager: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- A working GitHub CLI (`gh`) for integration tests that hit the GitHub API.

## Development Setup

Clone the repository, run `uv sync --group dev`, and use `uv run ...` for local commands. The virtualenv is created automatically at `.venv/`.

## Running Tests

The standard checks are `uv run pytest tests/`, `uv run ruff check src/ tests/`, `uv run ruff format src/ tests/`, and `uv run mypy src/`. The full suite runs under `pytest-xdist` (`-n auto`) by default; use `-p no:xdist` for serial debugging.

## Submitting a PR

1. Open an issue first for any non-trivial change (new plays, RL changes, schema changes). Describe the problem and your proposed approach. Wait for a maintainer to comment before investing implementation time.
2. Branch off `main` using the naming convention below.
3. Keep PRs focused: one bug fix or feature per PR. Schema changes land in their own PR.
4. Reference related issues with `Closes #<issue>` or `Related to #<issue>`.
5. All CI checks must pass before merge.
6. At least one maintainer review is required.

## Commit Messages

Concise, imperative-mood subject lines:

No ticket numbers in commit messages — link issues in the PR description.

## Branch Model

| Branch | Purpose |
|---|---|
| `main` | Active development — all PRs target here |

Tag releases from `main`. Releases follow [CalVer](https://calver.org/) (`YYYY.MM.PATCH`).

## Branch Naming

| Type | Pattern | Example |
|---|---|---|
| Feature | `feature/<short-description>` | `feature/warmup-gate` |
| Bug fix | `fix/<short-description>` | `fix/selector-config-index` |
| Refactor | `refactor/<short-description>` | `refactor/strip-ensure-methods` |
| Docs | `docs/<short-description>` | `docs/contributing-guide` |
| Schema | `schema/<short-description>` | `schema/worktrees-table` |

## Project Layout

| Path | Purpose |
|---|---|
| `src/agentshore/` | Core orchestrator package |
| `src/agentshore/rl/` | PPO policy, masking, observation encoder, reward |
| `src/agentshore/plays/` | Play implementations (22-action head, 19 active plays) |
| `src/agentshore/agents/` | Agent manager, CLI subprocess wrappers, worktrees |
| `src/agentshore/data/` | SQLite store, schema, migrations |
| `src/agentshore/core/` | Orchestrator base class and mixin stack |
| `src/agentshore/cli/` | Click-based CLI entry point |
| `src/agentshore/skills/templates/` | Skill prompt templates deployed to agent tool dirs |
| `dashboard/` | Vite + React dashboard (TypeScript) |
| `desktop/` | Tauri v2 desktop shell |
| `tests/` | pytest suite (parallel via xdist) |
| `docs/` | Design docs and PRD |

Read `CLAUDE.md` / `AGENTS.md` for architecture conventions, gotchas, and rules that the CI cannot enforce.

## Code Style

See `CLAUDE.md` for full conventions. Key rules:

- `from __future__ import annotations` in every module.
- Ruff for lint and format (line length 100).
- mypy strict mode — all new code must be fully annotated.
- asyncio for all I/O — no blocking calls in the core loop.
- SQLite via aiosqlite only — never raw sqlite3.
- No comments that restate what the code does — only WHY when non-obvious.
- Bug found → write a failing regression test first, then fix.

## Reporting Issues

Use [GitHub Issues](https://github.com/SuperSwinkAI/Swink-AgentShore/issues). Include:

- What you expected to happen
- What actually happened
- A minimal reproduction (config snippet, test case, or log excerpt)
- Python version (`python --version`) and OS
