"""Master availability record at ``~/.config/swink/agentshore/availability.yaml``.

Persisted inventory of "what's installable / authenticatable on this
machine." Both the agent-tier picker and the identity wizard refresh + read
this on every run, so the user-facing candidate lists come from a single
source instead of being re-detected per prompt.

Lives next to ``~/.config/swink/agentshore/sessions/`` and ``~/.config/swink/agentshore/weights/``.
"""

from __future__ import annotations

import contextlib
import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

from agentshore.agents.model_tiers import default_model_tiers_for
from agentshore.config import (
    AgentTypeAvailability,
    AvailabilityRecord,
    GhAccountAvailability,
)
from agentshore.environment import detect_agent_binaries, resolve_executable
from agentshore.identity_wizard import detect_gh_accounts
from agentshore.paths import GLOBAL_AVAILABILITY_PATH as DEFAULT_AVAILABILITY_PATH
from agentshore.state import AgentType

_VIA_GH_LOGIN = "gh_token_login"

# Detection speaks CLI binary names ("claude"); the rest of the system speaks
# AgentType ("claude_code"). One conversion layer here, not scattered.
_BINARY_TO_AGENT_TYPE: dict[str, AgentType] = {
    "claude": AgentType.CLAUDE_CODE,
    "codex": AgentType.CODEX,
    "grok": AgentType.GROK,
    "grok-build": AgentType.GROK,
    "agy": AgentType.ANTIGRAVITY,
}


def _empty_record() -> AvailabilityRecord:
    return AvailabilityRecord(last_refreshed=datetime.now(UTC).isoformat(timespec="seconds"))


def load(path: Path = DEFAULT_AVAILABILITY_PATH) -> AvailabilityRecord:
    """Read the persisted record. Returns an empty record on missing/malformed."""
    try:
        if not path.exists():
            return _empty_record()
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return _empty_record()
    if not isinstance(raw, dict):
        return _empty_record()
    return _record_from_dict(raw)


def save(record: AvailabilityRecord, path: Path = DEFAULT_AVAILABILITY_PATH) -> None:
    """yaml.safe_dump the record; create parent dir if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(dataclasses.asdict(record), sort_keys=False))


def refresh(path: Path = DEFAULT_AVAILABILITY_PATH) -> AvailabilityRecord:
    """Run detection now, persist, return.

    Detection cost (gh auth status, PATH lookup, tier inventory) is paid
    once per call. Wizards call this at entry; non-wizard code paths can
    use ``load()`` to read the cached record without paying it.
    """
    detected_binaries = detect_agent_binaries()
    detected_binary_set = set(detected_binaries)

    agent_rows: list[AgentTypeAvailability] = []
    seen_agent_types: set[AgentType] = set()
    for _binary, agent_type in _BINARY_TO_AGENT_TYPE.items():
        if agent_type in seen_agent_types:
            continue
        seen_agent_types.add(agent_type)
        aliases = [name for name, mapped in _BINARY_TO_AGENT_TYPE.items() if mapped == agent_type]
        detected_binary = next((name for name in aliases if name in detected_binary_set), None)
        present = detected_binary is not None
        binary_path = resolve_executable(detected_binary) if detected_binary is not None else None
        tiers = tuple(default_model_tiers_for(agent_type).keys()) if present else ()
        agent_rows.append(
            AgentTypeAvailability(
                agent_type=agent_type.value,
                binary=binary_path,
                available_tiers=tiers,
                available=present,
            )
        )

    accounts = detect_gh_accounts()
    account_rows = tuple(
        GhAccountAvailability(
            login=acc.login,
            active=acc.active,
            # Best-effort: default any gh-authed account to "gh_token_login"
            # (the safe ``gh auth token -u <login>`` path); we don't probe
            # keychain/env here.
            token_via=_VIA_GH_LOGIN,
        )
        for acc in accounts
    )

    record = AvailabilityRecord(
        last_refreshed=datetime.now(UTC).isoformat(timespec="seconds"),
        agent_types=tuple(agent_rows),
        github_accounts=account_rows,
    )
    with contextlib.suppress(OSError):
        save(record, path)
    return record


# Reading stays defensive: on-disk YAML is untrusted (hand-edited, partial, or
# from an older build). Writing is just ``dataclasses.asdict`` (see ``save``).


def _record_from_dict(raw: Mapping[str, object]) -> AvailabilityRecord:
    """Build an ``AvailabilityRecord`` from an untrusted YAML mapping."""
    last_refreshed = str(raw.get("last_refreshed", ""))

    raw_agents = raw.get("agent_types") or []
    agent_iter: list[object] = list(raw_agents) if isinstance(raw_agents, list) else []
    agent_rows = tuple(
        AgentTypeAvailability(
            agent_type=str(a.get("agent_type", "")),
            binary=a["binary"] if isinstance(a.get("binary"), str) else None,
            available_tiers=tuple(
                t for t in (a.get("available_tiers") or ()) if isinstance(t, str)
            ),
            available=bool(a.get("available", False)),
        )
        for a in agent_iter
        if isinstance(a, dict)
    )

    raw_accounts = raw.get("github_accounts") or []
    account_iter: list[object] = list(raw_accounts) if isinstance(raw_accounts, list) else []
    account_rows = tuple(
        GhAccountAvailability(
            login=str(g.get("login", "")),
            active=bool(g.get("active", False)),
            token_via=str(g.get("token_via", "gh_token_login")),
        )
        for g in account_iter
        if isinstance(g, dict)
    )
    return AvailabilityRecord(
        last_refreshed=last_refreshed or datetime.now(UTC).isoformat(timespec="seconds"),
        agent_types=agent_rows,
        github_accounts=account_rows,
    )
