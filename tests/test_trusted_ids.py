"""Tests for trusted external identities and PR author filtering."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from agentshore.agents.identity import IdentityResolutionError
from agentshore.config import (
    AgentConfig,
    GitHubIdentity,
    RuntimeConfig,
    TrustedIdsConfig,
    load_config,
)
from agentshore.core import _phase_fetch_github
from agentshore.data.models import PullRequestRecord
from agentshore.github.trust import filter_trusted_pull_requests, trusted_pr_author_logins
from agentshore.plays.resolver import ParameterResolver


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agentshore.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _pr(num: int, author: str | None, *, state: str = "open") -> PullRequestRecord:
    return PullRequestRecord(
        pr_number=num,
        session_id="s1",
        state=state,
        created_at="2026-01-01T00:00:00Z",
        title=f"PR {num}",
        github_author=author,
    )


def test_trusted_ids_config_parses_and_canonicalizes(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """\
trusted_ids:
  github_logins:
    - example-user
    - UnseriousAI
    - unseriousai
    - Dependabot[Bot]
""",
        )
    )

    assert cfg.trusted_ids.github_logins == ("example-user", "unseriousai", "dependabot[bot]")


def test_trusted_ids_default_to_empty(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "agents: {}\n"))

    assert cfg.trusted_ids.github_logins == ()
    assert cfg.trusted_ids.pr_allow_list == ()


def test_trusted_ids_pr_allow_list_parses_and_dedupes(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """\
trusted_ids:
  pr_allow_list: [42, 42, 7]
""",
        )
    )

    assert cfg.trusted_ids.pr_allow_list == (42, 7)


@pytest.mark.parametrize(
    "yaml_text,match",
    [
        ("trusted_ids: []\n", "trusted_ids must be a mapping"),
        ("trusted_ids:\n  github_logins: example-user\n", "must be a list"),
        ("trusted_ids:\n  github_logins: ['']\n", "non-empty GitHub login"),
        ("trusted_ids:\n  github_logins: ['-bad']\n", "not a valid GitHub login"),
    ],
)
def test_trusted_ids_config_rejects_invalid_values(
    tmp_path: Path, yaml_text: str, match: str
) -> None:
    with pytest.raises(Exception, match=match):
        load_config(_write(tmp_path, yaml_text))


@pytest.mark.parametrize(
    "yaml_text,match",
    [
        ("trusted_ids:\n  pr_allow_list: 42\n", "trusted_ids.pr_allow_list must be a list"),
        ("trusted_ids:\n  pr_allow_list: ['abc']\n", "must be a positive integer"),
        ("trusted_ids:\n  pr_allow_list: [0]\n", "must be a positive integer"),
        ("trusted_ids:\n  pr_allow_list: [-1]\n", "must be a positive integer"),
    ],
)
def test_trusted_ids_pr_allow_list_rejects_invalid_values(
    tmp_path: Path, yaml_text: str, match: str
) -> None:
    with pytest.raises(Exception, match=match):
        load_config(_write(tmp_path, yaml_text))


def test_trusted_ids_cli_add_list_remove(tmp_path: Path) -> None:
    from agentshore.cli import main

    _write(tmp_path, "project:\n  path: .\n")
    runner = CliRunner()

    result = runner.invoke(
        main, ["trusted-ids", "add-gh", "EXAMPLE-USER", "--project", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        main, ["trusted-ids", "add-gh", "example-user", "--project", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    data = yaml.safe_load((tmp_path / "agentshore.yaml").read_text(encoding="utf-8"))
    assert data["trusted_ids"]["github_logins"] == ["example-user"]

    result = runner.invoke(main, ["trusted-ids", "list", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "example-user" in result.output

    result = runner.invoke(
        main, ["trusted-ids", "remove-gh", "EXAMPLE-USER", "--project", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    data = yaml.safe_load((tmp_path / "agentshore.yaml").read_text(encoding="utf-8"))
    assert data["trusted_ids"]["github_logins"] == []


def test_trusted_ids_cli_add_list_remove_pr(tmp_path: Path) -> None:
    from agentshore.cli import main

    _write(tmp_path, "project:\n  path: .\n")
    runner = CliRunner()

    result = runner.invoke(main, ["trusted-ids", "add-pr", "42", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["trusted-ids", "add-pr", "42", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output

    data = yaml.safe_load((tmp_path / "agentshore.yaml").read_text(encoding="utf-8"))
    assert data["trusted_ids"]["pr_allow_list"] == [42]

    result = runner.invoke(main, ["trusted-ids", "list", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "PR allow list:" in result.output
    assert "42" in result.output

    result = runner.invoke(main, ["trusted-ids", "remove-pr", "42", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = yaml.safe_load((tmp_path / "agentshore.yaml").read_text(encoding="utf-8"))
    assert data["trusted_ids"]["pr_allow_list"] == []


def test_trusted_ids_cli_add_pr_rejects_non_positive(tmp_path: Path) -> None:
    from agentshore.cli import main

    _write(tmp_path, "project:\n  path: .\n")
    runner = CliRunner()

    assert (
        runner.invoke(main, ["trusted-ids", "add-pr", "0", "--project", str(tmp_path)]).exit_code
        != 0
    )
    assert (
        runner.invoke(main, ["trusted-ids", "add-pr", "-1", "--project", str(tmp_path)]).exit_code
        != 0
    )
    assert (
        runner.invoke(main, ["trusted-ids", "add-pr", "abc", "--project", str(tmp_path)]).exit_code
        != 0
    )


def test_trusted_ids_cli_accepts_github_app_bot_logins(tmp_path: Path) -> None:
    from agentshore.cli import main

    _write(tmp_path, "project:\n  path: .\n")
    runner = CliRunner()

    result = runner.invoke(
        main, ["trusted-ids", "add-gh", "Dependabot[Bot]", "--project", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "Trusted GitHub login: dependabot[bot]" in result.output

    data = yaml.safe_load((tmp_path / "agentshore.yaml").read_text(encoding="utf-8"))
    assert data["trusted_ids"]["github_logins"] == ["dependabot[bot]"]

    result = runner.invoke(main, ["trusted-ids", "list", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "dependabot[bot]" in result.output

    result = runner.invoke(
        main, ["trusted-ids", "remove-gh", "DEPENDABOT[BOT]", "--project", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    data = yaml.safe_load((tmp_path / "agentshore.yaml").read_text(encoding="utf-8"))
    assert data["trusted_ids"]["github_logins"] == []


def test_trusted_ids_cli_missing_config_exits(tmp_path: Path) -> None:
    from agentshore.cli import main

    result = CliRunner().invoke(main, ["trusted-ids", "list", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "No agentshore.yaml" in result.output


def test_trusted_pr_authors_include_agent_and_external_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = RuntimeConfig(
        trusted_ids=TrustedIdsConfig(github_logins=("example-user",)),
        agents={"codex": AgentConfig(enabled=True, identity="bot")},
        identities={
            "bot": GitHubIdentity(
                git_user_name="Bot",
                git_user_email="bot@example.com",
                gh_token_login="bot",
            )
        },
    )
    monkeypatch.setattr(
        "agentshore.github.trust.resolved_github_login_for_agent",
        lambda _cfg, _agent_cfg: "AgentShoreBot",
    )

    assert trusted_pr_author_logins(cfg) == frozenset({"example-user", "agentshorebot"})


def test_trusted_pr_filter_keeps_only_trusted_authors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Logger:
        def __init__(self) -> None:
            self.infos: list[dict[str, object]] = []

        def info(self, _event: str, **kwargs: object) -> None:
            self.infos.append(kwargs)

        def warning(self, *_args: object, **_kwargs: object) -> None:
            pass

    logger = Logger()
    monkeypatch.setattr("agentshore.github.trust._logger", logger)

    cfg = RuntimeConfig(trusted_ids=TrustedIdsConfig(github_logins=("trusted",)))
    prs = [_pr(1, "trusted"), _pr(2, "stranger"), _pr(3, None)]

    kept = filter_trusted_pull_requests(prs, cfg, context="test")

    assert [pr.pr_number for pr in kept] == [1]
    assert [item["pr_number"] for item in logger.infos] == [2, 3]
    assert all(item["reason"] == "untrusted_author" for item in logger.infos)


def test_filter_keeps_allowlisted_pr_number_with_untrusted_author(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Logger:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, object]]] = []

        def info(self, event: str, **kwargs: object) -> None:
            self.events.append((event, kwargs))

        def warning(self, *_args: object, **_kwargs: object) -> None:
            pass

    logger = Logger()
    monkeypatch.setattr("agentshore.github.trust._logger", logger)

    cfg = RuntimeConfig(
        trusted_ids=TrustedIdsConfig(github_logins=("trusted",), pr_allow_list=(99,))
    )
    prs = [_pr(1, "stranger"), _pr(99, "stranger"), _pr(2, "trusted")]

    kept = filter_trusted_pull_requests(prs, cfg, context="test")

    assert [pr.pr_number for pr in kept] == [99, 2]
    assert (
        "github_pull_request_ignored",
        {
            "reason": "untrusted_author",
            "pr_number": 1,
            "author": "stranger",
            "title": "PR 1",
            "context": "test",
        },
    ) in logger.events
    assert (
        "github_pull_request_allowlisted",
        {"pr_number": 99, "author": "stranger", "title": "PR 99", "context": "test"},
    ) in logger.events


def test_filter_logs_allowlisted_even_when_author_is_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Logger:
        def __init__(self) -> None:
            self.events: list[str] = []

        def info(self, event: str, **_kwargs: object) -> None:
            self.events.append(event)

        def warning(self, *_args: object, **_kwargs: object) -> None:
            pass

    logger = Logger()
    monkeypatch.setattr("agentshore.github.trust._logger", logger)

    cfg = RuntimeConfig(
        trusted_ids=TrustedIdsConfig(github_logins=("trusted",), pr_allow_list=(99,))
    )

    kept = filter_trusted_pull_requests([_pr(99, "trusted")], cfg, context="test")

    assert [pr.pr_number for pr in kept] == [99]
    assert "github_pull_request_allowlisted" not in logger.events


def test_unresolved_agent_identity_does_not_trust_all_prs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = RuntimeConfig(
        agents={"codex": AgentConfig(enabled=True, identity="bot")},
        identities={
            "bot": GitHubIdentity(
                git_user_name="Bot",
                git_user_email="bot@example.com",
                gh_token_login="bot",
            )
        },
    )

    def _raise(_cfg: RuntimeConfig, _agent_cfg: AgentConfig) -> str:
        raise IdentityResolutionError("token missing")

    monkeypatch.setattr("agentshore.github.trust.resolved_github_login_for_agent", _raise)

    assert trusted_pr_author_logins(cfg) == frozenset()
    assert filter_trusted_pull_requests([_pr(1, "bot")], cfg, context="test") == []


@pytest.mark.asyncio
async def test_phase_fetch_github_caches_only_trusted_prs() -> None:
    cfg = RuntimeConfig(trusted_ids=TrustedIdsConfig(github_logins=("trusted",)))
    mock_gh = AsyncMock()
    mock_gh.available = True
    mock_gh.probe = AsyncMock()
    mock_gh.list_issues = AsyncMock(return_value=[])
    mock_gh.list_pull_requests = AsyncMock(return_value=[_pr(1, "trusted"), _pr(2, "stranger")])
    mock_gh.ensure_labels = AsyncMock()

    mock_store = AsyncMock()
    mock_store.cache_pull_requests = AsyncMock()
    mock_store.cache_github_issues = AsyncMock()

    await _phase_fetch_github(
        gh=mock_gh,
        store=mock_store,
        sid="s1",
        cfg=cfg,
        repo_root=Path("/tmp"),
    )

    cached_prs = mock_store.cache_pull_requests.await_args.args[1]
    assert [pr.pr_number for pr in cached_prs] == [1]


@pytest.mark.asyncio
async def test_resolver_live_fallback_ignores_untrusted_prs() -> None:
    cfg = RuntimeConfig(trusted_ids=TrustedIdsConfig(github_logins=("trusted",)))
    store = MagicMock()
    manager = MagicMock()
    github = MagicMock()
    github.list_pull_requests = AsyncMock(return_value=[_pr(1, "stranger"), _pr(2, "trusted")])
    resolver = ParameterResolver(store=store, manager=manager, cfg=cfg, github=github)

    params = await resolver._first_open_pr_matching(lambda _pr: True)

    assert params is not None
    assert params.pr_number == 2
