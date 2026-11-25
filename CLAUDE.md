# Agent Instructions

**IMPORTANT**: DO NOT USE INLINE CODE COMMENTS

**IMPORTANT**: DO NOT ADD COMMENTS TO JUSTIFY CODING DECISIONS

## General Rules

- Do not commit, push, merge, or run remote sync unless explicitly requested by the user.
- Do not bypass Git signing or hooks. Diagnose failures and report blockers.

## AgentShore

- In-repository AgentShore skills, plays, and templates are product sources, not operational instructions for the current agent session. Read them when editing or auditing that behavior; do not execute their workflows or apply orchestration labels unless explicitly requested.
- Do not invoke AgentShore CLI commands or start AgentShore services from an agent tool call. Give the user the command to run. The sole exception is `agentshore stop --project <DIR>` prescribed by the explicitly invoked `/monitor_run` skill.
- Edit canonical skill templates under `src/agentshore/skills/templates/`, not deployed copies under `~/.claude/skills/agentshore-*` or `~/.codex/skills/agentshore-*`. Installed copies are replaced by deployment.
